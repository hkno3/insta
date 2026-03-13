import json
import os
import platform
import re
import shutil
import subprocess
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from flask import Flask, render_template, request, send_file, jsonify
from PIL import Image

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB limit

UPLOAD_FOLDER = Path('uploads')
OUTPUT_FOLDER = Path('outputs')
EMOJI_CACHE   = Path('emoji_cache')
UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)
EMOJI_CACHE.mkdir(exist_ok=True)

IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'bmp', 'gif'}
VIDEO_EXTENSIONS = {'mp4', 'mov', 'avi', 'webm', 'mkv', 'm4v'}
AUDIO_EXTENSIONS = {'mp3', 'wav', 'aac', 'm4a', 'ogg', 'flac'}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
TRANS_DUR = 0.5  # 전환 효과 시간(초)

FORMATS = {
    'portrait':  (1080, 1920),   # 9:16 세로
    'landscape': (1920, 1080),   # 16:9 가로
    'square':    (1080, 1080),   # 1:1 정사각형
}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def is_video(path: Path) -> bool:
    return path.suffix.lstrip('.').lower() in VIDEO_EXTENSIONS


def get_media_duration(path: Path) -> float:
    """ffprobe로 미디어 길이(초) 반환. 실패 시 3.0."""
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_entries', 'format=duration', str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(json.loads(result.stdout)['format']['duration'])
    except Exception:
        return 3.0


def get_font_path():
    """한글/영어 지원 폰트 경로 찾기."""
    system = platform.system()
    if system == 'Windows':
        candidates = [
            r'C:\Windows\Fonts\malgun.ttf',
            r'C:\Windows\Fonts\malgunbd.ttf',
            r'C:\Windows\Fonts\gulim.ttc',
            r'C:\Windows\Fonts\arial.ttf',
        ]
    elif system == 'Darwin':
        candidates = [
            '/System/Library/Fonts/AppleSDGothicNeo.ttc',
            '/System/Library/Fonts/Helvetica.ttc',
        ]
    else:
        candidates = [
            '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
        ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def escape_drawtext(text: str) -> str:
    text = text.replace('\\', '\\\\')
    text = text.replace(':', '\\:')
    text = text.replace("'", "\\'")
    text = text.replace('%', '\\%')
    return text


def _to_ffmpeg_color(color: str) -> str:
    """HTML #RRGGBB → ffmpeg 0xRRGGBB (@ opacity 부분은 그대로 유지)."""
    if '@' in color:
        col, alpha = color.split('@', 1)
        return (_to_ffmpeg_color(col) + '@' + alpha)
    return ('0x' + color[1:]) if color.startswith('#') else color


def build_caption_filter(text: str, height: int = VIDEO_HEIGHT,
                          font_size: int = 55, color: str = 'white',
                          box_color: str = 'black@0.5',
                          x_rel: float = 0.5, y_rel: float = 0.88) -> str:
    if not text or not text.strip():
        return ''
    font_path = get_font_path()
    escaped = escape_drawtext(text.strip())
    fp = font_path.replace('\\', '/') if font_path else ''
    if len(fp) >= 2 and fp[1] == ':':
        fp = fp[0] + '\\:' + fp[2:]
    font_part = f"fontfile='{fp}':" if fp else ''
    fc = _to_ffmpeg_color(color)
    if not box_color or box_color == 'none':
        box_part = 'box=0'
    else:
        bc = _to_ffmpeg_color(box_color)
        box_part = f'box=1:boxcolor={bc}:boxborderw=14'
    x_expr = f'(w*{x_rel:.4f}-text_w/2)'
    y_expr = f'(h*{y_rel:.4f})'
    return (
        f"drawtext={font_part}"
        f"text='{escaped}':"
        f"fontcolor={fc}:"
        f"fontsize={font_size}:"
        f"x={x_expr}:"
        f"y={y_expr}:"
        f"{box_part}"
    )


def prepare_image(src_path: Path, dst_path: Path,
                  width: int = VIDEO_WIDTH, height: int = VIDEO_HEIGHT):
    from PIL import ImageOps
    img = ImageOps.exif_transpose(Image.open(src_path)).convert('RGB')
    src_w, src_h = img.size
    target_ratio = width / height
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    else:
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))

    img = img.resize((width, height), Image.LANCZOS)
    img.save(dst_path, 'JPEG', quality=95)


def run_ffmpeg(cmd: list) -> tuple[bool, str]:
    result = subprocess.run(cmd, capture_output=True)
    stderr = result.stderr.decode('utf-8', errors='ignore') if result.stderr else ''
    if result.returncode != 0:
        app.logger.error(f"ffmpeg failed:\n{stderr}")
    return result.returncode == 0, stderr


# ── 이모지 스티커 ──────────────────────────────────────────

def emoji_to_codepoint(emoji: str) -> str:
    """이모지 문자 → twemoji 파일명 코드포인트 문자열."""
    return '-'.join(f'{ord(c):x}' for c in emoji)


def get_twemoji_png(emoji: str) -> Path | None:
    """Twemoji CDN에서 PNG를 다운로드하고 캐시. 실패 시 None 반환."""
    cp = emoji_to_codepoint(emoji)
    cache_path = EMOJI_CACHE / f'{cp}.png'
    if cache_path.exists():
        return cache_path
    base = 'https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72'
    try:
        urllib.request.urlretrieve(f'{base}/{cp}.png', cache_path)
        return cache_path
    except Exception:
        pass
    # FE0F(변형 선택자) 없이 재시도
    cp2 = '-'.join(p for p in cp.split('-') if p != 'fe0f')
    if cp2 != cp:
        cache_path2 = EMOJI_CACHE / f'{cp2}.png'
        if cache_path2.exists():
            return cache_path2
        try:
            urllib.request.urlretrieve(f'{base}/{cp2}.png', cache_path2)
            return cache_path2
        except Exception:
            pass
    app.logger.warning(f'twemoji 다운로드 실패: {emoji} ({cp})')
    return None


# ── 클립 생성 ──────────────────────────────────────────────

def _build_valid_stickers(stickers):
    valid = []
    for s in (stickers or []):
        png = get_twemoji_png(s['emoji'])
        if png:
            valid.append({**s, 'png': str(png)})
    return valid


def _sticker_filter_complex(valid: list, base_filter: str,
                             width: int = VIDEO_WIDTH,
                             height: int = VIDEO_HEIGHT) -> tuple[list, str]:
    """filter_complex 문자열 구성. (fc_parts, last_label) 반환."""
    fc = [f'[0:v]{base_filter}[base]']
    prev = 'base'
    for idx, s in enumerate(valid):
        size_px = max(40, int(float(s.get('size', 0.15)) * width))
        x_px    = max(0, min(width  - size_px, int(float(s['x']) * width)))
        y_px    = max(0, min(height - size_px, int(float(s['y']) * height)))
        fc.append(f'[{idx+1}:v]scale={size_px}:{size_px}[s{idx}]')
        out = f'v{idx}'
        fc.append(f'[{prev}][s{idx}]overlay={x_px}:{y_px}[{out}]')
        prev = out
    return fc, prev


def make_image_clip(img_path: Path, clip_path: Path, duration: float,
                    caption: str = '', stickers: list | None = None,
                    width: int = VIDEO_WIDTH,
                    height: int = VIDEO_HEIGHT,
                    caption_style: dict | None = None) -> tuple[bool, str]:
    """이미지 → 클립."""
    style     = caption_style or {}
    font_size = int(style.get('fontSize', 55))
    color     = style.get('color', 'white') or 'white'
    box_color = style.get('boxColor', 'black@0.5')
    x_rel     = float(style.get('captionX', 0.5))
    y_rel     = float(style.get('captionY', 0.88))
    cap   = build_caption_filter(caption, height, font_size, color, box_color, x_rel, y_rel)
    valid = _build_valid_stickers(stickers)

    if not valid:
        filters = [f'scale={width}:{height}']
        if cap:
            filters.append(cap)
        cmd = [
            'ffmpeg', '-y', '-loop', '1', '-i', str(img_path),
            '-t', str(duration),
            '-vf', ','.join(filters),
            '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2',
            '-pix_fmt', 'yuv420p', '-r', '30',
            str(clip_path),
        ]
        return run_ffmpeg(cmd)

    cmd = ['ffmpeg', '-y', '-loop', '1', '-i', str(img_path)]
    for s in valid:
        cmd += ['-i', s['png']]
    base = f'scale={width}:{height}' + (f',{cap}' if cap else '')
    fc, last = _sticker_filter_complex(valid, base, width, height)
    cmd += [
        '-t', str(duration),
        '-filter_complex', ';'.join(fc),
        '-map', f'[{last}]',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2',
        '-pix_fmt', 'yuv420p', '-r', '30',
        str(clip_path),
    ]
    return run_ffmpeg(cmd)


def make_video_clip(video_path: Path, clip_path: Path,
                    caption: str = '', stickers: list | None = None,
                    width: int = VIDEO_WIDTH,
                    height: int = VIDEO_HEIGHT,
                    caption_style: dict | None = None) -> tuple[bool, str]:
    """동영상 → 클립 (비율 크롭·리스케일, 자막·스티커 포함)."""
    style     = caption_style or {}
    font_size = int(style.get('fontSize', 55))
    color     = style.get('color', 'white') or 'white'
    box_color = style.get('boxColor', 'black@0.5')
    x_rel     = float(style.get('captionX', 0.5))
    y_rel     = float(style.get('captionY', 0.88))
    cap   = build_caption_filter(caption, height, font_size, color, box_color, x_rel, y_rel)
    valid = _build_valid_stickers(stickers)
    scale_crop = (f'scale={width}:{height}'
                  f':force_original_aspect_ratio=increase,'
                  f'crop={width}:{height}')

    if not valid:
        vf = scale_crop + (f',{cap}' if cap else '')
        cmd = [
            'ffmpeg', '-y', '-i', str(video_path),
            '-vf', vf, '-an',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2',
            '-pix_fmt', 'yuv420p', '-r', '30',
            str(clip_path),
        ]
        return run_ffmpeg(cmd)

    cmd = ['ffmpeg', '-y', '-i', str(video_path)]
    for s in valid:
        cmd += ['-i', s['png']]
    base = scale_crop + (f',{cap}' if cap else '')
    fc, last = _sticker_filter_complex(valid, base, width, height)
    cmd += [
        '-filter_complex', ';'.join(fc),
        '-map', f'[{last}]', '-an',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2',
        '-pix_fmt', 'yuv420p', '-r', '30',
        str(clip_path),
    ]
    return run_ffmpeg(cmd)


def make_clip(media_path: Path, clip_path: Path, duration: float,
              caption: str = '', stickers: list | None = None,
              width: int = VIDEO_WIDTH,
              height: int = VIDEO_HEIGHT,
              caption_style: dict | None = None) -> tuple[bool, str]:
    if is_video(media_path):
        return make_video_clip(media_path, clip_path, caption, stickers, width, height, caption_style)
    return make_image_clip(media_path, clip_path, duration, caption, stickers, width, height, caption_style)


XFADE_TRANSITIONS = {
    'fade', 'wipeleft', 'wiperight', 'wipeup', 'wipedown',
    'slideleft', 'slideright', 'slideup', 'slidedown',
    'circlecrop', 'rectcrop', 'distance', 'fadeblack', 'fadewhite',
    'radial', 'smoothleft', 'smoothright', 'smoothup', 'smoothdown',
    'circleopen', 'circleclose', 'vertopen', 'vertclose',
    'horzopen', 'horzclose', 'dissolve', 'pixelize',
    'diagtl', 'diagtr', 'diagbl', 'diagbr',
    'hlslice', 'hrslice', 'vuslice', 'vdslice', 'hblur',
}


def merge_two_clips(clip_a: Path, clip_b: Path, output: Path,
                    transition: str, dur_a: float) -> tuple[bool, str, float]:
    """두 클립을 전환 효과로 합치기. (ok, err, 새 길이) 반환."""
    if transition == 'none':
        cmd = [
            'ffmpeg', '-y',
            '-i', str(clip_a), '-i', str(clip_b),
            '-filter_complex', '[0:v][1:v]concat=n=2:v=1[v]',
            '-map', '[v]',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2',
            '-pix_fmt', 'yuv420p',
            str(output),
        ]
        ok, err = run_ffmpeg(cmd)
        return ok, err, dur_a

    xfade_name = transition if transition in XFADE_TRANSITIONS else 'fade'
    offset = max(dur_a - TRANS_DUR, 0)
    cmd = [
        'ffmpeg', '-y',
        '-i', str(clip_a), '-i', str(clip_b),
        '-filter_complex',
        f'[0:v][1:v]xfade=transition={xfade_name}:duration={TRANS_DUR}:offset={offset}[v]',
        '-map', '[v]',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2',
        '-pix_fmt', 'yuv420p',
        '-r', '30',
        str(output),
    ]
    ok, err = run_ffmpeg(cmd)
    return ok, err, dur_a - TRANS_DUR


def make_video(media_paths: list[Path], output_path: Path,
               img_duration: float, transitions: list[str],
               captions: list[str] | None = None,
               stickers_per_photo: list[list] | None = None,
               width: int = VIDEO_WIDTH,
               height: int = VIDEO_HEIGHT,
               caption_styles: list[dict] | None = None,
               photo_durations: list | None = None) -> tuple[bool, str]:
    n = len(media_paths)
    if captions is None:
        captions = [''] * n
    while len(captions) < n:
        captions.append('')
    while len(transitions) < n:
        transitions.append('none')
    if stickers_per_photo is None:
        stickers_per_photo = [[] for _ in range(n)]
    while len(stickers_per_photo) < n:
        stickers_per_photo.append([])
    if caption_styles is None:
        caption_styles = [{} for _ in range(n)]
    while len(caption_styles) < n:
        caption_styles.append({})

    tmp_dir = output_path.parent / f'clips_{output_path.stem}'
    tmp_dir.mkdir(exist_ok=True)

    clips = []
    clip_durations = []
    for i, (media, cap) in enumerate(zip(media_paths, captions)):
        clip = tmp_dir / f'clip_{i:03d}.mp4'
        if is_video(media):
            dur = get_media_duration(media)
        elif photo_durations and i < len(photo_durations) and photo_durations[i]:
            dur = float(photo_durations[i])
        else:
            dur = img_duration
        ok, err = make_clip(media, clip, dur, cap, stickers_per_photo[i], width, height, caption_styles[i])
        if not ok:
            _cleanup(tmp_dir)
            return False, err
        clips.append(clip)
        clip_durations.append(dur)

    if n == 1:
        shutil.copy2(clips[0], output_path)
        _cleanup(tmp_dir)
        return True, ''

    current = clips[0]
    current_dur = clip_durations[0]

    for i in range(1, n):
        trans = transitions[i - 1]
        merged = tmp_dir / f'merged_{i:03d}.mp4'
        ok, err, _ = merge_two_clips(current, clips[i], merged, trans, current_dur)
        if not ok:
            _cleanup(tmp_dir)
            return False, err
        current = merged
        # 이론값 누적 대신 실제 길이 측정 → xfade offset 오차 누적 방지
        current_dur = get_media_duration(merged)

    shutil.copy2(current, output_path)
    _cleanup(tmp_dir)
    return True, ''


def compute_sfx_start_times(media_paths: list[Path], img_duration: float,
                             transitions: list[str],
                             photo_durations: list | None = None) -> list[float]:
    """각 클립의 최종 영상 내 시작 시간(초) 계산."""
    starts = [0.0]
    for i in range(len(media_paths) - 1):
        if is_video(media_paths[i]):
            dur = get_media_duration(media_paths[i])
        elif photo_durations and i < len(photo_durations) and photo_durations[i]:
            dur = float(photo_durations[i])
        else:
            dur = img_duration
        trans = transitions[i] if i < len(transitions) else 'none'
        overlap = TRANS_DUR if trans != 'none' else 0.0
        starts.append(starts[-1] + dur - overlap)
    return starts


def add_audio_to_video(video_path: Path, output_path: Path,
                       sfx_list: list | None = None,
                       bg_music: Path | None = None,
                       bg_volume: float = 1.0,
                       bg_start: float = 0.0,
                       bg_end: float = 0.0,
                       photo_music_list: list | None = None) -> tuple[bool, str]:
    """영상에 배경음악·효과음·사진별배경음 합성.
    photo_music_list: [(path, start_sec, duration, volume, trim_start, trim_end), ...]
    trim_start/trim_end: 0 means no trim (use full file)"""
    cmd = ['ffmpeg', '-y', '-i', str(video_path)]
    fc = []
    labels = []
    idx = 1
    _seg_path = None

    if bg_music:
        if bg_end > bg_start:
            _seg_path = bg_music.parent / 'bgm_segment.aac'
            run_ffmpeg([
                'ffmpeg', '-y', '-i', str(bg_music),
                '-ss', f'{bg_start:.2f}', '-t', f'{(bg_end - bg_start):.2f}',
                '-vn', '-c:a', 'aac', '-b:a', '128k', str(_seg_path),
            ])
            cmd += ['-stream_loop', '-1', '-i', str(_seg_path)]
        else:
            cmd += ['-stream_loop', '-1', '-ss', f'{bg_start:.2f}', '-i', str(bg_music)]
        fc.append(f'[{idx}:a]volume={bg_volume:.2f}[bgm]')
        labels.append('[bgm]')
        idx += 1

    for sfx_path, start_sec in (sfx_list or []):
        delay_ms = int(start_sec * 1000)
        cmd += ['-i', str(sfx_path)]
        lbl = f'sfx{idx}'
        fc.append(f'[{idx}:a]adelay={delay_ms}|{delay_ms}[{lbl}]')
        labels.append(f'[{lbl}]')
        idx += 1

    # 사진별 배경음: (path, start_sec, duration_sec, volume[, trim_start, trim_end])
    _pm_seg_paths = []
    for pm_entry in (photo_music_list or []):
        pm_path, pm_start, pm_dur, pm_vol = pm_entry[:4]
        pm_trim_start = float(pm_entry[4]) if len(pm_entry) > 4 else 0.0
        pm_trim_end   = float(pm_entry[5]) if len(pm_entry) > 5 else 0.0
        delay_ms = int(pm_start * 1000)
        lbl = f'pm{idx}'
        if pm_trim_start > 0 or pm_trim_end > 0:
            # extract trimmed segment to temp file
            _pm_seg = pm_path.parent / f'pm_seg_{idx}.aac'
            seg_cmd = ['ffmpeg', '-y', '-i', str(pm_path), '-ss', f'{pm_trim_start:.3f}']
            if pm_trim_end > pm_trim_start:
                seg_cmd += ['-t', f'{(pm_trim_end - pm_trim_start):.3f}']
            seg_cmd += ['-vn', '-c:a', 'aac', '-b:a', '128k', str(_pm_seg)]
            run_ffmpeg(seg_cmd)
            _pm_seg_paths.append(_pm_seg)
            cmd += ['-i', str(_pm_seg)]
        else:
            cmd += ['-i', str(pm_path)]
        fc.append(
            f'[{idx}:a]atrim=end={pm_dur:.3f},asetpts=PTS-STARTPTS,'
            f'adelay={delay_ms}|{delay_ms},volume={pm_vol:.2f}[{lbl}]'
        )
        labels.append(f'[{lbl}]')
        idx += 1

    if not labels:
        for _ps in _pm_seg_paths:
            if _ps.exists(): _ps.unlink(missing_ok=True)
        return True, ''

    if len(labels) == 1:
        out_label = labels[0].strip('[]')
    else:
        out_label = 'aout'
        fc.append(f'{"".join(labels)}amix=inputs={len(labels)}:normalize=0[{out_label}]')

    cmd += [
        '-filter_complex', ';'.join(fc),
        '-map', '0:v', '-map', f'[{out_label}]',
        '-shortest',
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
        str(output_path),
    ]
    ok, err = run_ffmpeg(cmd)

    if _seg_path and _seg_path.exists():
        _seg_path.unlink(missing_ok=True)
    for _ps in _pm_seg_paths:
        if _ps.exists():
            _ps.unlink(missing_ok=True)

    return ok, err


def _cleanup(tmp_dir: Path):
    for f in tmp_dir.iterdir():
        f.unlink(missing_ok=True)
    tmp_dir.rmdir()


RSS_URL = 'https://bodyandwell.com/feed'

@app.route('/img-proxy')
def img_proxy():
    from flask import Response
    url = request.args.get('url', '')
    if not url or not url.startswith('http'):
        return '', 400
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://bodyandwell.com/'})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = r.read()
            ctype = r.headers.get('Content-Type', 'image/jpeg')
        return Response(data, content_type=ctype)
    except Exception:
        return '', 404


@app.route('/rss-feed')
def rss_feed():
    try:
        req = urllib.request.Request(RSS_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=6) as r:
            raw = r.read()
        root = ET.fromstring(raw)
        ns = {'media': 'http://search.yahoo.com/mrss/', 'dc': 'http://purl.org/dc/elements/1.1/'}
        items = []
        for item in root.iter('item'):
            title = (item.findtext('title') or '').strip()
            link  = (item.findtext('link') or '').strip()
            desc_raw = item.findtext('description') or ''
            desc  = re.sub(r'<[^>]+>', '', desc_raw).strip()[:120]
            # 썸네일: media:content > enclosure > description 내 img
            thumb = None
            mc = item.find('media:content', ns)
            if mc is not None:
                thumb = mc.get('url')
            if not thumb:
                enc = item.find('enclosure')
                if enc is not None and (enc.get('type') or '').startswith('image'):
                    thumb = enc.get('url')
            if not thumb:
                m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc_raw)
                if m:
                    thumb = m.group(1)
            if title and link:
                items.append({'title': title, 'link': link, 'desc': desc, 'thumb': thumb})
            if len(items) >= 10:
                break
        return jsonify(items)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/create', methods=['POST'])
def create_video():
    files = request.files.getlist('photos')
    music_file = request.files.get('music')
    music_volume = float(request.form.get('music_volume', 1.0))
    music_start  = float(request.form.get('music_start', 0.0))
    music_end_raw = request.form.get('music_end', '')
    music_end    = float(music_end_raw) if music_end_raw else 0.0
    duration = float(request.form.get('duration', 3.0))
    captions_json        = request.form.get('captions',        '[]')
    transitions_json     = request.form.get('transitions',     '[]')
    stickers_json        = request.form.get('stickers',        '[]')
    caption_styles_json  = request.form.get('caption_styles',  '[]')
    photo_durations_json = request.form.get('photo_durations', '[]')
    fmt = request.form.get('format', 'portrait')
    width, height = FORMATS.get(fmt, (VIDEO_WIDTH, VIDEO_HEIGHT))

    try:
        captions = json.loads(captions_json)
    except Exception:
        captions = []
    try:
        transitions = json.loads(transitions_json)
    except Exception:
        transitions = []
    try:
        stickers_per_photo = json.loads(stickers_json)
    except Exception:
        stickers_per_photo = []
    try:
        caption_styles = json.loads(caption_styles_json)
    except Exception:
        caption_styles = []
    try:
        photo_durations = json.loads(photo_durations_json)
    except Exception:
        photo_durations = []

    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': '파일을 하나 이상 업로드해 주세요.'}), 400

    if duration < 1 or duration > 10:
        return jsonify({'error': '사진당 재생 시간은 1~10초 사이여야 합니다.'}), 400

    job_id = uuid.uuid4().hex
    job_dir = UPLOAD_FOLDER / job_id
    job_dir.mkdir()

    prepared = []
    for i, f in enumerate(files):
        if not (f and allowed_file(f.filename)):
            continue
        ext = f.filename.rsplit('.', 1)[1].lower()
        raw_path = job_dir / f'raw_{i:03d}.{ext}'
        f.save(raw_path)
        if ext in VIDEO_EXTENSIONS:
            # 동영상은 그대로 사용
            prepared.append(raw_path)
        else:
            prepared_path = job_dir / f'img_{i:03d}.jpg'
            try:
                prepare_image(raw_path, prepared_path, width, height)
                prepared.append(prepared_path)
            except Exception as e:
                return jsonify({'error': f'이미지 처리 오류: {e}'}), 500

    if not prepared:
        return jsonify({'error': '유효한 파일이 없습니다.'}), 400

    video_path = OUTPUT_FOLDER / f'{job_id}.mp4'
    success, err_msg = make_video(
        prepared, video_path, duration, transitions, captions,
        stickers_per_photo, width, height, caption_styles, photo_durations
    )

    if not success:
        snippet = err_msg[-800:] if err_msg else "ffmpeg 오류"
        return jsonify({'error': f'영상 생성 실패: {snippet}'}), 500

    # 배경음악 저장
    bg_path = None
    if music_file and music_file.filename:
        music_ext = music_file.filename.rsplit('.', 1)[-1].lower()
        if music_ext in AUDIO_EXTENSIONS:
            bg_path = job_dir / f'music.{music_ext}'
            music_file.save(bg_path)

    # 효과음 + 사진별 배경음 수집
    sfx_list         = []
    photo_music_list = []
    sfx_keys = [k for k in request.files if k.startswith('sfx_')]
    pm_keys  = [k for k in request.files if k.startswith('photo_music_')]

    if sfx_keys or pm_keys:
        start_times = compute_sfx_start_times(prepared, duration, transitions, photo_durations)

        for key in sfx_keys:
            try:
                photo_idx = int(key.split('_', 1)[1])
            except ValueError:
                continue
            f = request.files[key]
            if not f.filename or photo_idx >= len(prepared):
                continue
            ext = f.filename.rsplit('.', 1)[-1].lower()
            if ext in AUDIO_EXTENSIONS:
                sfx_path = job_dir / f'{key}.{ext}'
                f.save(sfx_path)
                t = start_times[photo_idx] if photo_idx < len(start_times) else 0.0
                sfx_list.append((sfx_path, t))

        for key in pm_keys:
            try:
                photo_idx = int(key.split('_')[-1])
            except ValueError:
                continue
            f = request.files[key]
            if not f.filename or photo_idx >= len(prepared):
                continue
            ext = f.filename.rsplit('.', 1)[-1].lower()
            if ext not in AUDIO_EXTENSIONS:
                continue
            pm_path = job_dir / f'{key}.{ext}'
            f.save(pm_path)
            pm_vol = float(request.form.get(f'photo_music_vol_{photo_idx}', 1.0))
            pm_trim_start = float(request.form.get(f'photo_music_trim_start_{photo_idx}', 0.0))
            pm_trim_end   = float(request.form.get(f'photo_music_trim_end_{photo_idx}',   0.0))
            if is_video(prepared[photo_idx]):
                pm_dur = get_media_duration(prepared[photo_idx])
            elif photo_durations and photo_idx < len(photo_durations) and photo_durations[photo_idx]:
                pm_dur = float(photo_durations[photo_idx])
            else:
                pm_dur = duration
            pm_start = start_times[photo_idx] if photo_idx < len(start_times) else 0.0
            photo_music_list.append((pm_path, pm_start, pm_dur, pm_vol, pm_trim_start, pm_trim_end))

    # 오디오 합성
    if bg_path or sfx_list or photo_music_list:
        audio_out = OUTPUT_FOLDER / f'{job_id}_audio.mp4'
        ok, err = add_audio_to_video(video_path, audio_out, sfx_list,
                                     bg_path, music_volume, music_start, music_end,
                                     photo_music_list)
        if ok:
            video_path.unlink(missing_ok=True)
            audio_out.rename(video_path)
        else:
            app.logger.warning(f'오디오 추가 실패, 음소거로 반환: {err[-300:]}')

    return jsonify({'video_id': job_id})


@app.route('/preview/<video_id>')
def preview(video_id):
    if not video_id.isalnum() or len(video_id) != 32:
        return 'Invalid ID', 400
    path = OUTPUT_FOLDER / f'{video_id}.mp4'
    if not path.exists():
        return 'Not found', 404
    return send_file(path, mimetype='video/mp4')


@app.route('/download/<video_id>')
def download(video_id):
    if not video_id.isalnum() or len(video_id) != 32:
        return 'Invalid ID', 400
    path = OUTPUT_FOLDER / f'{video_id}.mp4'
    if not path.exists():
        return 'Not found', 404
    return send_file(path, as_attachment=True, download_name='instagram_video.mp4')


if __name__ == '__main__':
    app.run(debug=True)
