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
    """ffmpeg drawtext 필터용 텍스트 이스케이프."""
    text = text.replace('\\', '\\\\')
    text = text.replace(':', '\\:')
    text = text.replace("'", "\\'")
    text = text.replace('%', '\\%')
    return text


def build_caption_filter(text: str, font_size: int = 55) -> str:
    """자막 drawtext 필터 문자열 생성."""
    if not text or not text.strip():
        return ''
    font_path = get_font_path()
    escaped = escape_drawtext(text.strip())
    fp = font_path.replace('\\', '/') if font_path else ''
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
    """이미지를 1080x1920에 맞게 크롭 후 저장."""
    img = Image.open(src_path).convert('RGB')
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


def make_clip(img_path: Path, clip_path: Path, duration: float,
              caption: str = '', extra_vf: str = '') -> bool:
    """단일 이미지로 클립 생성 (자막 포함 가능)."""
    filters = [f'scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}']
    if extra_vf:
        filters.append(extra_vf)
    cap = build_caption_filter(caption)
    if cap:
        filters.append(cap)
    vf = ','.join(filters)

    cmd = [
        'ffmpeg', '-y',
        '-loop', '1', '-i', str(img_path),
        '-t', str(duration),
        '-vf', vf,
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-r', '30',
        str(clip_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def concat_clips(clip_paths: list[Path], output_path: Path) -> bool:
    """클립들을 순서대로 이어붙이기."""
    concat_list = output_path.parent / f'concat_{output_path.stem}.txt'
    with open(concat_list, 'w', encoding='utf-8') as f:
        for c in clip_paths:
            f.write(f"file '{c.resolve()}'\n")

    cmd = [
        'ffmpeg', '-y',
        '-f', 'concat', '-safe', '0',
        '-i', str(concat_list),
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    concat_list.unlink(missing_ok=True)
    return result.returncode == 0


def make_video(image_paths: list[Path], output_path: Path,
               duration: float, transition: str,
               captions: list[str] | None = None) -> bool:
    """ffmpeg를 사용해 이미지 슬라이드쇼 영상 생성."""
    if captions is None:
        captions = [''] * len(image_paths)
    # 길이 맞추기
    while len(captions) < len(image_paths):
        captions.append('')

    if transition == 'fade':
        return _make_fade_video(image_paths, output_path, duration, captions)
    elif transition == 'slide':
        return _make_slide_video(image_paths, output_path, duration, captions)
    else:
        return _make_simple_video(image_paths, output_path, duration, captions)


def _make_simple_video(image_paths, output_path, duration, captions):
    """전환 효과 없는 슬라이드쇼."""
    tmp_dir = output_path.parent / f'clips_{output_path.stem}'
    tmp_dir.mkdir(exist_ok=True)
    clips = []

    for i, (img_path, caption) in enumerate(zip(image_paths, captions)):
        clip_path = tmp_dir / f'clip_{i:03d}.mp4'
        if not make_clip(img_path, clip_path, duration, caption):
            return False
        clips.append(clip_path)

    success = concat_clips(clips, output_path)
    for c in clips:
        c.unlink(missing_ok=True)
    tmp_dir.rmdir()
    return success


def _make_fade_video(image_paths, output_path, duration, captions):
    """페이드 인/아웃 전환 효과."""
    fade_dur = 0.5
    tmp_dir = output_path.parent / f'clips_{output_path.stem}'
    tmp_dir.mkdir(exist_ok=True)
    clips = []

    for i, (img_path, caption) in enumerate(zip(image_paths, captions)):
        clip_path = tmp_dir / f'clip_{i:03d}.mp4'
        fade_vf = (
            f'fade=t=in:st=0:d={fade_dur},'
            f'fade=t=out:st={duration - fade_dur}:d={fade_dur}'
        )
        if not make_clip(img_path, clip_path, duration, caption, fade_vf):
            return False
        clips.append(clip_path)

    success = concat_clips(clips, output_path)
    for c in clips:
        c.unlink(missing_ok=True)
    tmp_dir.rmdir()
    return success


def _make_slide_video(image_paths, output_path, duration, captions):
    """슬라이드 전환 효과."""
    n = len(image_paths)
    trans_dur = 0.4
    fps = 30

    if n == 1:
        return _make_simple_video(image_paths, output_path, duration, captions)

    tmp_dir = output_path.parent / f'clips_{output_path.stem}'
    tmp_dir.mkdir(exist_ok=True)
    clips = []

    for i, (img_path, caption) in enumerate(zip(image_paths, captions)):
        clip_path = tmp_dir / f'clip_{i:03d}.mp4'
        if not make_clip(img_path, clip_path, duration, caption):
            return False
        clips.append(clip_path)

    current = clips[0]
    offset = duration - trans_dur

    for i in range(1, n):
        merged = tmp_dir / f'merged_{i:03d}.mp4'
        cmd = [
            'ffmpeg', '-y',
            '-i', str(current),
            '-i', str(clips[i]),
            '-filter_complex',
            f'[0:v][1:v]xfade=transition=slideleft:duration={trans_dur}:offset={offset}[v]',
            '-map', '[v]',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            '-r', str(fps),
            str(merged),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            for c in clips:
                c.unlink(missing_ok=True)
            tmp_dir.rmdir()
            return _make_simple_video(image_paths, output_path, duration, captions)
        current = merged
        offset += duration - trans_dur

    shutil.copy2(current, output_path)
    for c in clips:
        c.unlink(missing_ok=True)
    for f in tmp_dir.iterdir():
        f.unlink(missing_ok=True)
    tmp_dir.rmdir()
    return True


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/create', methods=['POST'])
def create_video():
    files = request.files.getlist('photos')
    duration = float(request.form.get('duration', 3.0))
    transition = request.form.get('transition', 'fade')
    captions_json = request.form.get('captions', '[]')

    try:
        captions = json.loads(captions_json)
    except Exception:
        captions = []

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
    success = make_video(prepared, output_path, duration, transition, captions)

    if not success:
        return jsonify({'error': '영상 생성에 실패했습니다. ffmpeg 설치 여부를 확인해 주세요.'}), 500

    return jsonify({'video_id': job_id})


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
