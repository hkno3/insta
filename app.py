import json
import os
import platform
import shutil
import subprocess
import uuid
from pathlib import Path

from flask import Flask, render_template, request, send_file, jsonify
from PIL import Image

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB limit

UPLOAD_FOLDER = Path('uploads')
OUTPUT_FOLDER = Path('outputs')
UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)

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


def make_clip(img_path: Path, clip_path: Path, duration: float, caption: str = '') -> tuple[bool, str]:
    """단일 이미지 → 클립 (자막 포함)."""
    filters = [f'scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}']
    cap = build_caption_filter(caption)
    if cap:
        filters.append(cap)
    cmd = [
        'ffmpeg', '-y',
        '-loop', '1', '-i', str(img_path),
        '-t', str(duration),
        '-vf', ','.join(filters),
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-r', '30',
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
        return ok, err, dur_a  # duration은 concat 후 ffprobe로 정확히 알 수 있지만 근사치 사용

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
               captions: list[str] | None = None) -> tuple[bool, str]:
    """
    transitions[i] = 사진 i에서 사진 i+1로 넘어갈 때의 전환 효과
    ('fade' | 'slide' | 'none')
    """
    n = len(image_paths)
    if captions is None:
        captions = [''] * n
    while len(captions) < n:
        captions.append('')
    while len(transitions) < n:
        transitions.append('none')

    tmp_dir = output_path.parent / f'clips_{output_path.stem}'
    tmp_dir.mkdir(exist_ok=True)

    # 1단계: 개별 클립 생성
    clips = []
    for i, (img, cap) in enumerate(zip(image_paths, captions)):
        clip = tmp_dir / f'clip_{i:03d}.mp4'
        ok, err = make_clip(img, clip, duration, cap)
        if not ok:
            _cleanup(tmp_dir)
            return False, err
        clips.append(clip)

    if n == 1:
        shutil.copy2(clips[0], output_path)
        _cleanup(tmp_dir)
        return True, ''

    # 2단계: 순서대로 전환 효과 적용
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
        current_dur += duration  # 근사 누적

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
    captions_json = request.form.get('captions', '[]')
    transitions_json = request.form.get('transitions', '[]')

    try:
        captions = json.loads(captions_json)
    except Exception:
        captions = []
    try:
        transitions = json.loads(transitions_json)
    except Exception:
        transitions = []

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
    success, err_msg = make_video(prepared, output_path, duration, transitions, captions)

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
