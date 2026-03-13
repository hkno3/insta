import json
import os
import platform
import shutil
import subprocess
import urllib.request
import uuid
from pathlib import Path

from flask import Flask, render_template, request, send_file, jsonify
from PIL import Image

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB limit

UPLOAD_FOLDER = Path('uploads')
OUTPUT_FOLDER = Path('outputs')
EMOJI_CACHE   = Path('emoji_cache')
UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)
EMOJI_CACHE.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'bmp', 'gif'}
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
TRANS_DUR = 0.5  # 전환 효과 시간(초)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


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


def build_caption_filter(text: str, font_size: int = 55) -> str:
    if not text or not text.strip():
        return ''
    font_path = get_font_path()
    escaped = escape_drawtext(text.strip())
    fp = font_path.replace('\\', '/') if font_path else ''
    if len(fp) >= 2 and fp[1] == ':':
        fp = fp[0] + '\\:' + fp[2:]
    font_part = f"fontfile='{fp}':" if fp else ''
    y_pos = VIDEO_HEIGHT - font_size * 3
    return (
        f"drawtext={font_part}"
        f"text='{escaped}':"
        f"fontcolor=white:"
        f"fontsize={font_size}:"
        f"x=(w-text_w)/2:"
        f"y={y_pos}:"
        f"box=1:boxcolor=black@0.5:boxborderw=14"
    )


def prepare_image(src_path: Path, dst_path: Path):
    from PIL import ImageOps
    img = ImageOps.exif_transpose(Image.open(src_path)).convert('RGB')
    src_w, src_h = img.size
    target_ratio = VIDEO_WIDTH / VIDEO_HEIGHT
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    else:
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))

    img = img.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.LANCZOS)
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

def make_clip(img_path: Path, clip_path: Path, duration: float,
              caption: str = '', stickers: list | None = None) -> tuple[bool, str]:
    """단일 이미지 → 클립 (자막 + 이모지 스티커 포함)."""
    cap = build_caption_filter(caption)

    # 유효한 스티커만 필터링 (PNG 다운로드 성공한 것만)
    valid = []
    for s in (stickers or []):
        png = get_twemoji_png(s['emoji'])
        if png:
            valid.append({**s, 'png': str(png)})

    if not valid:
        # 스티커 없음 → 단순 -vf
        filters = [f'scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}']
        if cap:
            filters.append(cap)
        cmd = [
            'ffmpeg', '-y',
            '-loop', '1', '-i', str(img_path),
            '-t', str(duration),
            '-vf', ','.join(filters),
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', '30',
            str(clip_path),
        ]
        return run_ffmpeg(cmd)

    # 스티커 있음 → filter_complex
    cmd = ['ffmpeg', '-y', '-loop', '1', '-i', str(img_path)]
    for s in valid:
        cmd += ['-i', s['png']]

    base_filter = f'scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}'
    if cap:
        base_filter += f',{cap}'

    fc = [f'[0:v]{base_filter}[base]']
    prev = 'base'
    for idx, s in enumerate(valid):
        size_px = max(40, int(float(s.get('size', 0.15)) * VIDEO_WIDTH))
        x_px    = max(0, min(VIDEO_WIDTH  - size_px, int(float(s['x']) * VIDEO_WIDTH)))
        y_px    = max(0, min(VIDEO_HEIGHT - size_px, int(float(s['y']) * VIDEO_HEIGHT)))
        fc.append(f'[{idx+1}:v]scale={size_px}:{size_px}[s{idx}]')
        out = f'v{idx}'
        fc.append(f'[{prev}][s{idx}]overlay={x_px}:{y_px}[{out}]')
        prev = out

    cmd += [
        '-t', str(duration),
        '-filter_complex', ';'.join(fc),
        '-map', f'[{prev}]',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', '30',
        str(clip_path),
    ]
    return run_ffmpeg(cmd)


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
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
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
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-r', '30',
        str(output),
    ]
    ok, err = run_ffmpeg(cmd)
    return ok, err, dur_a - TRANS_DUR


def make_video(image_paths: list[Path], output_path: Path,
               duration: float, transitions: list[str],
               captions: list[str] | None = None,
               stickers_per_photo: list[list] | None = None) -> tuple[bool, str]:
    n = len(image_paths)
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

    tmp_dir = output_path.parent / f'clips_{output_path.stem}'
    tmp_dir.mkdir(exist_ok=True)

    clips = []
    for i, (img, cap) in enumerate(zip(image_paths, captions)):
        clip = tmp_dir / f'clip_{i:03d}.mp4'
        ok, err = make_clip(img, clip, duration, cap, stickers_per_photo[i])
        if not ok:
            _cleanup(tmp_dir)
            return False, err
        clips.append(clip)

    if n == 1:
        shutil.copy2(clips[0], output_path)
        _cleanup(tmp_dir)
        return True, ''

    current = clips[0]
    current_dur = duration

    for i in range(1, n):
        trans = transitions[i - 1]
        merged = tmp_dir / f'merged_{i:03d}.mp4'
        ok, err, current_dur = merge_two_clips(current, clips[i], merged, trans, current_dur)
        if not ok:
            _cleanup(tmp_dir)
            return False, err
        current = merged
        current_dur += duration

    shutil.copy2(current, output_path)
    _cleanup(tmp_dir)
    return True, ''


def _cleanup(tmp_dir: Path):
    for f in tmp_dir.iterdir():
        f.unlink(missing_ok=True)
    tmp_dir.rmdir()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/create', methods=['POST'])
def create_video():
    files = request.files.getlist('photos')
    duration = float(request.form.get('duration', 3.0))
    captions_json    = request.form.get('captions',    '[]')
    transitions_json = request.form.get('transitions', '[]')
    stickers_json    = request.form.get('stickers',    '[]')

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

    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': '사진을 하나 이상 업로드해 주세요.'}), 400

    if duration < 1 or duration > 10:
        return jsonify({'error': '사진당 재생 시간은 1~10초 사이여야 합니다.'}), 400

    job_id = uuid.uuid4().hex
    job_dir = UPLOAD_FOLDER / job_id
    job_dir.mkdir()

    prepared = []
    for i, f in enumerate(files):
        if f and allowed_file(f.filename):
            ext = f.filename.rsplit('.', 1)[1].lower()
            raw_path = job_dir / f'raw_{i:03d}.{ext}'
            f.save(raw_path)
            prepared_path = job_dir / f'img_{i:03d}.jpg'
            try:
                prepare_image(raw_path, prepared_path)
                prepared.append(prepared_path)
            except Exception as e:
                return jsonify({'error': f'이미지 처리 오류: {e}'}), 500

    if not prepared:
        return jsonify({'error': '유효한 이미지 파일이 없습니다.'}), 400

    output_path = OUTPUT_FOLDER / f'{job_id}.mp4'
    success, err_msg = make_video(
        prepared, output_path, duration, transitions, captions, stickers_per_photo
    )

    if not success:
        return jsonify({'error': f'영상 생성 실패: {err_msg[:300] if err_msg else "ffmpeg 오류"}'}), 500

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
