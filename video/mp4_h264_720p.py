import subprocess
import shutil
import json
import time
import sys
import os
import re
import platform
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Configuration ---
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".ts"}
THRESHOLD = 720
COOLDOWN_SECONDS = 60
CPU_THREADS = 2  # Limit to 2 threads for ~50% CPU usage on i5-4570T
IS_WINDOWS = platform.system() == "Windows"


def set_terminal_title(title):
    try:
        if IS_WINDOWS:
            subprocess.run(["title", title], shell=True)
        else:
            sys.stdout.write(f"\x1b]2;{title}\x07")
            sys.stdout.flush()
    except Exception:
        pass


def is_faststart(filepath):
    # Check if 'moov' atom appears before 'mdat' atom
    try:
        with open(filepath, "rb") as f:
            while True:
                header = f.read(8)
                if len(header) < 8:
                    break
                size = int.from_bytes(header[:4], "big")
                atom_type = header[4:8]

                if atom_type == b"moov":
                    return True
                if atom_type == b"mdat":
                    return False

                # Skip to the next atom
                f.seek(size - 8, 1)
    except Exception:
        pass
    return False


def get_video_audio_info(file_path):
    # Added pix_fmt, r_frame_rate, avg_frame_rate to detect 10-bit and VFR
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=width,height,codec_name,codec_type,pix_fmt,r_frame_rate,avg_frame_rate",
        "-of",
        "json",
        str(file_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)

        v_w, v_h, v_codec = 0, 0, "unknown"
        a_codec = "none"
        pix_fmt = "unknown"
        r_fps = "0/0"
        avg_fps = "0/0"

        for stream in data.get("streams", []):
            s_type = stream.get("codec_type")
            if s_type == "video" and v_codec == "unknown":
                v_w = int(stream.get("width", 0))
                v_h = int(stream.get("height", 0))
                v_codec = stream.get("codec_name", "unknown")
                pix_fmt = stream.get("pix_fmt", "unknown")
                r_fps = stream.get("r_frame_rate", "0/0")
                avg_fps = stream.get("avg_frame_rate", "0/0")
            elif s_type == "audio" and a_codec == "none":
                a_codec = stream.get("codec_name", "unknown")

        # Basic VFR heuristic: if base framerate differs from average framerate
        is_vfr = (r_fps != "0/0" and avg_fps != "0/0" and r_fps != avg_fps)
        return v_w, v_h, v_codec, a_codec, pix_fmt, is_vfr
    except:
        return 0, 0, "unknown", "unknown", "unknown", False


def process_videos(root_dir):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("Error: FFmpeg or ffprobe not found in PATH.")
        return

    set_terminal_title("H264 & 720P Optimizer")

    print(f"\n[*] Scanning file list in: {root_dir}")
    files_to_probe = [
        f
        for f in root_dir.rglob("*")
        if f.is_file()
        and f.suffix.lower() in VIDEO_EXTENSIONS
        and not f.name.endswith(".tmp.mp4")
    ]

    total_files = len(files_to_probe)
    if total_files == 0:
        print("[i] No files need optimization or transcoding.")
        return

    targets = []
    print(f"[*] Found {total_files} potential files. Starting concurrent probing...")

    def probe_task(file_path):
        w, h, v_codec, a_codec, pix_fmt, is_vfr = get_video_audio_info(file_path)
        short_side = min(w, h)

        needs_downscale = short_side > THRESHOLD
        needs_v_transcode = v_codec != "h264"
        needs_a_transcode = a_codec != "aac" and a_codec != "none"
        
        # Trigger transcode if it's 10-bit (e.g., yuv420p10le) or non-standard web format
        needs_pix_fmt_fix = "10" in pix_fmt or pix_fmt not in ("yuv420p", "nv12", "unknown")

        is_transcode = (
            needs_downscale 
            or needs_v_transcode 
            or needs_a_transcode 
            or needs_pix_fmt_fix 
            or is_vfr
        )

        needs_faststart = False
        if not is_transcode and file_path.suffix.lower() == ".mp4":
            needs_faststart = not is_faststart(file_path)

        return (
            file_path,
            w,
            h,
            v_codec,
            a_codec,
            is_transcode,
            needs_downscale,
            needs_a_transcode,
            needs_faststart,
        )

    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(probe_task, f): f for f in files_to_probe}

        completed = 0
        for future in as_completed(futures):
            completed += 1
            (
                file_path,
                w,
                h,
                v_codec,
                a_codec,
                is_transcode,
                needs_downscale,
                needs_a_transcode,
                needs_faststart,
            ) = future.result()

            print(f"\r    -> Probing ({completed}/{total_files})...", end="")
            dst = file_path.with_suffix(".mp4")

            if is_transcode:
                targets.append(
                    (
                        file_path,
                        w,
                        h,
                        v_codec,
                        a_codec,
                        dst,
                        True,
                        needs_downscale,
                        needs_a_transcode,
                    )
                )
            elif needs_faststart:
                targets.append(
                    (file_path, w, h, v_codec, a_codec, dst, False, False, False)
                )

    print("\n" + "-" * 50)
    total_targets = len(targets)
    if total_targets == 0:
        print("[i] All files are already optimized.")
        return

    print(
        f"[i] Filtered {total_targets} targets for execution. Thread limit: {CPU_THREADS}\n"
        + "-" * 50
    )

    for index, (
        src,
        w,
        h,
        v_codec,
        a_codec,
        dst,
        is_transcode,
        downscale,
        transcode_audio,
    ) in enumerate(targets):
        task_desc = f"Video: {w}x{h} {v_codec}, Audio: {a_codec}"
        if not is_transcode:
            task_desc = "Faststart Optimization Only"

        print(f"[+] Task {index + 1}/{total_targets} | {src.name} ({task_desc})")
        set_terminal_title(f"[{index+1}/{total_targets}] {src.name}")

        temp_dst = dst.with_suffix(".tmp.mp4")

        if is_transcode:
            audio_args = (
                ["-c:a", "aac", "-b:a", "128k"] if transcode_audio else ["-c:a", "copy"]
            )
            if a_codec == "none":
                audio_args = ["-an"]

            if IS_WINDOWS:
                filters = []
                if downscale:
                    filters.append(
                        f"scale=-2:{THRESHOLD}" if w >= h else f"scale={THRESHOLD}:-2"
                    )

                command = [
                    "ffmpeg", "-y", "-threads", str(CPU_THREADS), "-i", str(src)
                ]
                if filters:
                    command += ["-vf", ",".join(filters)]
                command += [
                    "-c:v", "h264_qsv",
                    "-global_quality", "23",
                    "-fps_mode", "cfr",
                    "-pix_fmt", "nv12",
                ] + audio_args + [
                    "-movflags", "+faststart", str(temp_dst)
                ]
            else:
                filters = ["format=nv12"]
                if downscale:
                    filters.append(
                        f"scale=-2:{THRESHOLD}" if w >= h else f"scale={THRESHOLD}:-2"
                    )

                command = (
                    [
                        "ffmpeg", "-y",
                        "-vaapi_device", "/dev/dri/renderD128",
                        "-threads", str(CPU_THREADS),
                        "-i", str(src),
                        "-vf", ",".join(filters) + ",hwupload",
                        "-c:v", "h264_vaapi",
                        "-qp", "23",
                        "-fps_mode", "cfr",
                    ]
                    + audio_args
                    + [
                        "-movflags", "+faststart", str(temp_dst)
                    ]
                )
        else:
            command = [
                "ffmpeg", "-y", "-v", "error", "-i", str(src),
                "-c", "copy", "-movflags", "+faststart", str(temp_dst)
            ]

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in process.stdout:
                if "frame=" in line:
                    print(f"\r    {line.strip()}", end="")
            process.wait()

            if process.returncode == 0:
                temp_dst.replace(dst)
                if src != dst and src.exists():
                    src.unlink()
                    print(f"\n[✔] Done (Replaced original {src.name})")
                else:
                    print(f"\n[✔] Done (Replaced original Mp4)")
            else:
                if temp_dst.exists():
                    temp_dst.unlink()
                print(f"\n[!] Failed (ffmpeg exit code {process.returncode})")
        except Exception as e:
            if temp_dst.exists():
                temp_dst.unlink()
            print(f"\n[!] Error: {e}")

        if index < total_targets - 1:
            if is_transcode:
                print(f"[i] Cooldown: {COOLDOWN_SECONDS}s...")
                time.sleep(COOLDOWN_SECONDS)
            print("-" * 50)

    print("\n[*] All optimization/transcoding tasks finished.\n")


def merge_sequential_videos(base_dir):
    print(f"\n[*] Scanning for sequential files to merge in: {base_dir}")
    merged_count = 0

    for root, dirs, files in os.walk(base_dir):
        folder_name = os.path.basename(root)
        seq_files = []

        pattern = re.compile(
            rf"^(?:{re.escape(folder_name)}[-_]?)?(\d+)(?:-(\d+))?\.mp4$", re.IGNORECASE
        )

        for f in files:
            match = pattern.match(f)
            if match:
                start_num = int(match.group(1))
                end_num = int(match.group(2)) if match.group(2) else start_num
                seq_files.append({"start": start_num, "end": end_num, "filename": f})

        if not seq_files:
            continue

        seq_files.sort(key=lambda x: (x["start"], -x["end"]))
        valid_chain = []
        expected_next_start = None

        for curr in seq_files:
            if expected_next_start is None:
                valid_chain.append(curr)
                expected_next_start = curr["end"] + 1
            else:
                if curr["start"] == expected_next_start:
                    valid_chain.append(curr)
                    expected_next_start = curr["end"] + 1
                elif curr["start"] < expected_next_start:
                    continue
                else:
                    break

        if len(valid_chain) < 2:
            continue

        min_num = valid_chain[0]["start"]
        max_num = valid_chain[-1]["end"]

        output = f"{folder_name}_{min_num}-{max_num}.mp4"
        output_path = os.path.join(root, output)

        if os.path.exists(output_path):
            continue

        inputs = [x["filename"] for x in valid_chain]
        print(f"\n[+] Merging {len(inputs)} files into {output} in {root}")
        ts_files = []

        for i, video in enumerate(inputs):
            ts_file = f"temp_{i}.ts"
            ts_files.append(ts_file)
            cmd_convert = [
                "ffmpeg", "-y", "-v", "error", "-i", video,
                "-c", "copy", "-bsf:v", "h264_mp4toannexb",
                "-f", "mpegts", ts_file
            ]
            subprocess.run(cmd_convert, cwd=root, check=True)

        concat_string = "concat:" + "|".join(ts_files)
        cmd_merge = [
            "ffmpeg", "-y", "-v", "error", "-i", concat_string,
            "-c", "copy", "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart", output
        ]
        subprocess.run(cmd_merge, cwd=root, check=True)

        for ts in ts_files:
            ts_path = os.path.join(root, ts)
            if os.path.exists(ts_path):
                os.remove(ts_path)

        print(f"[✔] Merged successfully: {output}")
        merged_count += 1

    if merged_count == 0:
        print("[i] No files need merging.")
    else:
        print(f"\n[*] All merging tasks finished. Total merged: {merged_count}")


if __name__ == "__main__":
    base_directory = Path.cwd()
    process_videos(base_directory)
    merge_sequential_videos(base_directory)