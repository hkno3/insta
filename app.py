import os
import subprocess
import tempfile
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


def prepare_image(src_path: Path, dst_path: Path):
    """이미지를 1080x1920에 맞게 크롭/패딩 처리 후 저장."""
    img = Image.open(src_path).convert('RGB')
    src_w, src_h = img.size
    target_ratio = VIDEO_WIDTH / VIDEO_HEIGHT
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        # 이미지가 더 넓음 → 좌우 크롭
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    else:
        # 이미지가 더 좁음 → 상하 크롭
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))

    img = img.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.LANCZOS)
    img.save(dst_path, 'JPEG', quality=95)


def make_video(image_paths: list[Path], output_path: Path, duration: float, transition: str) -> bool:
    """ffmpeg를 사용해 이미지 슬라이드쇼 영상 생성."""
    n = len(image_paths)

    if transition == 'fade':
        return _make_fade_video(image_paths, output_path, duration)
    elif transition == 'slide':
        return _make_slide_video(image_paths, output_path, duration)
    else:
        return _make_simple_video(image_paths, output_path, duration)


def _make_simple_video(image_paths: list[Path], output_path: Path, duration: float) -> bool:
    """단순 슬라이드쇼 (전환 효과 없음)."""
    concat_list = output_path.parent / f'concat_{output_path.stem}.txt'
    with open(concat_list, 'w') as f:
        for p in image_paths:
            f.write(f"file '{p.resolve()}'\n")
            f.write(f"duration {duration}\n")
        # ffmpeg concat demuxer는 마지막 이미지 재지정 필요
        f.write(f"file '{image_paths[-1].resolve()}'\n")

    cmd = [
        'ffmpeg', '-y',
        '-f', 'concat', '-safe', '0',
        '-i', str(concat_list),
        '-vf', f'scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-r', '30',
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    concat_list.unlink(missing_ok=True)
    return result.returncode == 0


def _make_fade_video(image_paths: list[Path], output_path: Path, duration: float) -> bool:
    """페이드 인/아웃 전환 효과."""
    n = len(image_paths)
    fade_dur = 0.5  # 페이드 시간(초)

    # 각 이미지를 개별 클립으로 만들기
    clips = []
    tmp_dir = output_path.parent / f'clips_{output_path.stem}'
    tmp_dir.mkdir(exist_ok=True)

    for i, img_path in enumerate(image_paths):
        clip_path = tmp_dir / f'clip_{i:03d}.mp4'
        fade_filter = (
            f'fade=t=in:st=0:d={fade_dur},'
            f'fade=t=out:st={duration - fade_dur}:d={fade_dur}'
        )
        cmd = [
            'ffmpeg', '-y',
            '-loop', '1', '-i', str(img_path),
            '-t', str(duration),
            '-vf', f'scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},{fade_filter}',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            '-r', '30',
            str(clip_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return False
        clips.append(clip_path)

    # 클립들을 concat
    concat_list = output_path.parent / f'concat_{output_path.stem}.txt'
    with open(concat_list, 'w') as f:
        for c in clips:
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
    for c in clips:
        c.unlink(missing_ok=True)
    tmp_dir.rmdir()

    return result.returncode == 0


def _make_slide_video(image_paths: list[Path], output_path: Path, duration: float) -> bool:
    """슬라이드(왼쪽→오른쪽) 전환 효과."""
    n = len(image_paths)
    trans_dur = 0.4  # 전환 시간(초)
    fps = 30
    w, h = VIDEO_WIDTH, VIDEO_HEIGHT

    if n == 1:
        return _make_simple_video(image_paths, output_path, duration)

    tmp_dir = output_path.parent / f'clips_{output_path.stem}'
    tmp_dir.mkdir(exist_ok=True)

    # 각 이미지를 개별 클립으로 변환
    clips = []
    for i, img_path in enumerate(image_paths):
        clip_path = tmp_dir / f'clip_{i:03d}.mp4'
        cmd = [
            'ffmpeg', '-y',
            '-loop', '1', '-i', str(img_path),
            '-t', str(duration),
            '-vf', f'scale={w}:{h}',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            '-r', str(fps),
            str(clip_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return False
        clips.append(clip_path)

    # xfade 필터로 클립 연결
    # xfade는 두 클립을 순차적으로 연결하는 방식
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
            # xfade 실패 시 단순 concat으로 폴백
            for c in clips:
                c.unlink(missing_ok=True)
            tmp_dir.rmdir()
            return _make_simple_video(image_paths, output_path, duration)
        current = merged
        offset += duration - trans_dur

    import shutil
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
    success = make_video(prepared, output_path, duration, transition)

    if not success:
        return jsonify({'error': '영상 생성에 실패했습니다. ffmpeg 설치 여부를 확인해 주세요.'}), 500

    return jsonify({'video_id': job_id})


@app.route('/download/<video_id>')
def download(video_id):
    # video_id는 hex uuid여야 함 (보안)
    if not video_id.isalnum() or len(video_id) != 32:
        return 'Invalid ID', 400
    path = OUTPUT_FOLDER / f'{video_id}.mp4'
    if not path.exists():
        return 'Not found', 404
    return send_file(path, as_attachment=True, download_name='instagram_video.mp4')


if __name__ == '__main__':
    app.run(debug=True)
