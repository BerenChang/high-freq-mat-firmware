import argparse
import os
import platform
import statistics
import sys
import time
from collections import deque

import serial
import serial.tools.list_ports


# ============================================================
# Pressure Mat Protocol
# ============================================================

START_READING_COMMAND = "start_reading"

MAT_SIZE = 1568
VERIFICATION_SEQUENCE = bytes([255, 254, 254, 255])
VERIFICATION_WIDTH = len(VERIFICATION_SEQUENCE)

FRAME_SIZE = MAT_SIZE + VERIFICATION_WIDTH


# ============================================================
# Utility Functions
# ============================================================

def now():
    return time.perf_counter()


def safe_mean(values):
    return statistics.mean(values) if values else 0.0


def safe_min(values):
    return min(values) if values else 0.0


def safe_max(values):
    return max(values) if values else 0.0


def percentile(values, p):
    if not values:
        return 0.0

    values = sorted(values)
    k = (len(values) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(values) - 1)

    if f == c:
        return values[f]

    return values[f] + (values[c] - values[f]) * (k - f)


def bytes_to_mbps(byte_rate):
    return byte_rate * 8.0 / 1_000_000.0


def fps_from_byte_rate(byte_rate):
    return byte_rate / FRAME_SIZE


def byte_rate_from_fps(fps):
    return fps * FRAME_SIZE


def uart_equivalent_baud(fps):
    """
    UART 8N1 uses roughly 10 bits per payload byte.
    This is only a UART-equivalent estimate.
    USB CDC does not necessarily obey this baud rate.
    """
    return fps * FRAME_SIZE * 10.0


def find_port_info(port_name):
    for p in serial.tools.list_ports.comports():
        if p.device == port_name:
            return p
    return None


def print_host_and_port_info(port, baud):
    print()
    print("========== Host / Port Info ==========")
    print(f"Python version:          {sys.version.split()[0]}")
    print(f"PySerial version:        {serial.VERSION}")
    print(f"Platform:                {platform.platform()}")
    print(f"Machine:                 {platform.machine()}")
    print(f"Processor:               {platform.processor()}")
    print(f"Process ID:              {os.getpid()}")
    print(f"Serial port:             {port}")
    print(f"Configured baud rate:    {baud}")

    port_info = find_port_info(port)

    if port_info is None:
        print("Port details:            Not available")
    else:
        print(f"Description:             {port_info.description}")
        print(f"HWID:                    {port_info.hwid}")
        print(f"VID:                     {port_info.vid}")
        print(f"PID:                     {port_info.pid}")
        print(f"Manufacturer:            {port_info.manufacturer}")
        print(f"Product:                 {port_info.product}")
        print(f"Serial number:           {port_info.serial_number}")
        print(f"USB location:            {port_info.location}")

    print("======================================")
    print()


def open_serial(port, baud, timeout):
    ser = serial.Serial(
        port=port,
        baudrate=baud,
        timeout=timeout,
        write_timeout=1.0,
    )

    try:
        ser.set_buffer_size(rx_size=FRAME_SIZE * 200, tx_size=4096)
        buffer_resize_ok = True
    except Exception:
        buffer_resize_ok = False

    ser.reset_input_buffer()
    ser.reset_output_buffer()

    return ser, buffer_resize_ok


def send_start_command(ser):
    ser.write((START_READING_COMMAND + "\n").encode("utf-8"))
    ser.flush()


def sync_to_next_frame(ser, timeout_s=3.0):
    """
    Reads byte-by-byte until FF FE FE FF is found.
    Since the marker is at the END of a frame, after this returns,
    the next byte should be the beginning of the next frame.
    """
    deadline = now() + timeout_s
    hist = bytearray()

    while now() < deadline:
        b = ser.read(1)

        if not b:
            continue

        hist += b

        if len(hist) > VERIFICATION_WIDTH:
            hist = hist[-VERIFICATION_WIDTH:]

        if bytes(hist) == VERIFICATION_SEQUENCE:
            return True

    return False


# ============================================================
# Test 1: Byte Drain Test
# ============================================================

def run_byte_drain_test(ser, duration, chunk_size):
    """
    Reads raw bytes as fast as possible.
    No frame parsing.
    No validation.
    This tests whether the host can drain the USB serial stream quickly.
    """
    print()
    print("========== Test 1: Byte Drain ==========")
    print("Purpose: raw host receive speed, without frame parsing.")
    print("Reading raw bytes as fast as possible...")
    print("========================================")

    ser.reset_input_buffer()
    time.sleep(0.2)

    total_bytes = 0
    read_calls = 0
    empty_reads = 0
    in_waiting_samples = []
    read_durations = []

    wall_start = now()
    cpu_start = time.process_time()
    last_report = wall_start

    while now() - wall_start < duration:
        try:
            waiting = ser.in_waiting
        except Exception:
            waiting = 0

        in_waiting_samples.append(waiting)

        read_size = waiting if waiting > 0 else chunk_size
        read_size = min(max(read_size, 1), chunk_size)

        t0 = now()
        data = ser.read(read_size)
        t1 = now()

        read_durations.append(t1 - t0)
        read_calls += 1

        if data:
            total_bytes += len(data)
        else:
            empty_reads += 1

        if t1 - last_report >= 1.0:
            elapsed = t1 - wall_start
            byte_rate = total_bytes / elapsed
            est_fps = fps_from_byte_rate(byte_rate)

            print(
                f"[{elapsed:6.2f}s] "
                f"raw rate: {byte_rate:10.1f} B/s | "
                f"estimated FPS from bytes: {est_fps:7.2f} Hz | "
                f"in_waiting: {waiting:6d}"
            )

            last_report = t1

    wall_time = now() - wall_start
    cpu_time = time.process_time() - cpu_start

    byte_rate = total_bytes / wall_time if wall_time > 0 else 0.0

    return {
        "name": "byte_drain",
        "duration": wall_time,
        "cpu_time": cpu_time,
        "cpu_percent_one_core": 100.0 * cpu_time / wall_time if wall_time > 0 else 0.0,
        "total_bytes": total_bytes,
        "byte_rate": byte_rate,
        "estimated_fps": fps_from_byte_rate(byte_rate),
        "read_calls": read_calls,
        "empty_reads": empty_reads,
        "avg_read_ms": safe_mean(read_durations) * 1000.0,
        "max_read_ms": safe_max(read_durations) * 1000.0,
        "avg_in_waiting": safe_mean(in_waiting_samples),
        "max_in_waiting": safe_max(in_waiting_samples),
    }


# ============================================================
# Test 2: Fixed Frame Read Test
# ============================================================

def run_frame_read_test(ser, duration):
    """
    Reads exactly FRAME_SIZE bytes per frame.
    Validates the verification sequence.
    This is close to your original code.
    """
    print()
    print("========== Test 2: Fixed Frame Read ==========")
    print("Purpose: your normal frame-by-frame reading method.")
    print("Syncing to frame boundary...")
    print("==============================================")

    ser.reset_input_buffer()
    time.sleep(0.2)

    if not sync_to_next_frame(ser):
        print("Could not sync to frame marker. Continuing anyway.")

    valid_frames = 0
    total_frames = 0
    total_bytes = 0
    errors = 0
    short_frames = 0
    timeouts = 0

    frame_intervals = []
    read_durations = []
    in_waiting_before_samples = []
    in_waiting_after_samples = []

    previous_frame_time = None

    wall_start = now()
    cpu_start = time.process_time()
    last_report = wall_start

    while now() - wall_start < duration:
        try:
            waiting_before = ser.in_waiting
        except Exception:
            waiting_before = 0

        in_waiting_before_samples.append(waiting_before)

        read_start = now()
        frame = ser.read(FRAME_SIZE)
        read_end = now()

        read_durations.append(read_end - read_start)

        try:
            waiting_after = ser.in_waiting
        except Exception:
            waiting_after = 0

        in_waiting_after_samples.append(waiting_after)

        if not frame:
            timeouts += 1
            continue

        total_bytes += len(frame)

        if len(frame) != FRAME_SIZE:
            short_frames += 1
            errors += 1
            sync_to_next_frame(ser, timeout_s=1.0)
            continue

        total_frames += 1

        if frame[-VERIFICATION_WIDTH:] != VERIFICATION_SEQUENCE:
            errors += 1
            ser.reset_input_buffer()
            sync_to_next_frame(ser, timeout_s=1.0)
            continue

        valid_frames += 1

        frame_time = read_end

        if previous_frame_time is not None:
            frame_intervals.append(frame_time - previous_frame_time)

        previous_frame_time = frame_time

        if read_end - last_report >= 1.0:
            elapsed = read_end - wall_start
            fps = valid_frames / elapsed
            byte_rate = total_bytes / elapsed

            print(
                f"[{elapsed:6.2f}s] "
                f"valid FPS: {fps:7.2f} Hz | "
                f"rate: {byte_rate:10.1f} B/s | "
                f"errors: {errors:3d} | "
                f"timeouts: {timeouts:3d} | "
                f"in_waiting after: {waiting_after:6d}"
            )

            last_report = read_end

    wall_time = now() - wall_start
    cpu_time = time.process_time() - cpu_start

    fps = valid_frames / wall_time if wall_time > 0 else 0.0
    byte_rate = total_bytes / wall_time if wall_time > 0 else 0.0

    return {
        "name": "fixed_frame_read",
        "duration": wall_time,
        "cpu_time": cpu_time,
        "cpu_percent_one_core": 100.0 * cpu_time / wall_time if wall_time > 0 else 0.0,
        "valid_frames": valid_frames,
        "total_frames": total_frames,
        "total_bytes": total_bytes,
        "fps": fps,
        "byte_rate": byte_rate,
        "errors": errors,
        "short_frames": short_frames,
        "timeouts": timeouts,
        "avg_frame_interval_ms": safe_mean(frame_intervals) * 1000.0,
        "min_frame_interval_ms": safe_min(frame_intervals) * 1000.0,
        "max_frame_interval_ms": safe_max(frame_intervals) * 1000.0,
        "p50_frame_interval_ms": percentile(frame_intervals, 50) * 1000.0,
        "p95_frame_interval_ms": percentile(frame_intervals, 95) * 1000.0,
        "p99_frame_interval_ms": percentile(frame_intervals, 99) * 1000.0,
        "avg_read_ms": safe_mean(read_durations) * 1000.0,
        "max_read_ms": safe_max(read_durations) * 1000.0,
        "avg_in_waiting_before": safe_mean(in_waiting_before_samples),
        "max_in_waiting_before": safe_max(in_waiting_before_samples),
        "avg_in_waiting_after": safe_mean(in_waiting_after_samples),
        "max_in_waiting_after": safe_max(in_waiting_after_samples),
    }


# ============================================================
# Test 3: Bulk Parse Test
# ============================================================

def run_bulk_parse_test(ser, duration, chunk_size):
    """
    Reads large chunks and detects frame markers in a rolling buffer.
    This removes much of the per-frame serial.read(FRAME_SIZE) overhead.
    """
    print()
    print("========== Test 3: Bulk Parse ==========")
    print("Purpose: faster host-side reading and marker parsing.")
    print("If this is much faster than Test 2, per-frame reading is the bottleneck.")
    print("========================================")

    ser.reset_input_buffer()
    time.sleep(0.2)

    buffer = bytearray()

    total_bytes = 0
    detected_frames = 0
    read_calls = 0
    empty_reads = 0

    frame_intervals = []
    read_durations = []
    in_waiting_samples = []

    previous_marker_time = None

    wall_start = now()
    cpu_start = time.process_time()
    last_report = wall_start

    while now() - wall_start < duration:
        try:
            waiting = ser.in_waiting
        except Exception:
            waiting = 0

        in_waiting_samples.append(waiting)

        read_size = waiting if waiting > 0 else chunk_size
        read_size = min(max(read_size, 1), chunk_size)

        t0 = now()
        data = ser.read(read_size)
        t1 = now()

        read_durations.append(t1 - t0)
        read_calls += 1

        if not data:
            empty_reads += 1
            continue

        total_bytes += len(data)
        buffer.extend(data)

        while True:
            idx = buffer.find(VERIFICATION_SEQUENCE)

            if idx < 0:
                # Keep enough tail bytes to avoid missing a marker split across reads
                if len(buffer) > FRAME_SIZE * 4:
                    buffer = buffer[-FRAME_SIZE * 2:]
                break

            detected_frames += 1

            marker_time = t1

            if previous_marker_time is not None:
                frame_intervals.append(marker_time - previous_marker_time)

            previous_marker_time = marker_time

            # Remove everything up to and including the marker
            del buffer[:idx + VERIFICATION_WIDTH]

        if t1 - last_report >= 1.0:
            elapsed = t1 - wall_start
            fps = detected_frames / elapsed
            byte_rate = total_bytes / elapsed

            print(
                f"[{elapsed:6.2f}s] "
                f"marker FPS: {fps:7.2f} Hz | "
                f"rate: {byte_rate:10.1f} B/s | "
                f"in_waiting: {waiting:6d}"
            )

            last_report = t1

    wall_time = now() - wall_start
    cpu_time = time.process_time() - cpu_start

    fps = detected_frames / wall_time if wall_time > 0 else 0.0
    byte_rate = total_bytes / wall_time if wall_time > 0 else 0.0

    return {
        "name": "bulk_parse",
        "duration": wall_time,
        "cpu_time": cpu_time,
        "cpu_percent_one_core": 100.0 * cpu_time / wall_time if wall_time > 0 else 0.0,
        "detected_frames": detected_frames,
        "total_bytes": total_bytes,
        "fps": fps,
        "byte_rate": byte_rate,
        "estimated_fps_from_bytes": fps_from_byte_rate(byte_rate),
        "read_calls": read_calls,
        "empty_reads": empty_reads,
        "avg_frame_interval_ms": safe_mean(frame_intervals) * 1000.0,
        "min_frame_interval_ms": safe_min(frame_intervals) * 1000.0,
        "max_frame_interval_ms": safe_max(frame_intervals) * 1000.0,
        "p50_frame_interval_ms": percentile(frame_intervals, 50) * 1000.0,
        "p95_frame_interval_ms": percentile(frame_intervals, 95) * 1000.0,
        "p99_frame_interval_ms": percentile(frame_intervals, 99) * 1000.0,
        "avg_read_ms": safe_mean(read_durations) * 1000.0,
        "max_read_ms": safe_max(read_durations) * 1000.0,
        "avg_in_waiting": safe_mean(in_waiting_samples),
        "max_in_waiting": safe_max(in_waiting_samples),
    }


# ============================================================
# Test 4: Local Parser Benchmark
# ============================================================

def run_local_parser_benchmark(duration):
    """
    Tests how fast this PC can parse frame markers from memory.
    This does not use serial at all.
    If this number is huge compared with measured FPS, Python parsing is not the bottleneck.
    """
    print()
    print("========== Test 4: Local Parser Benchmark ==========")
    print("Purpose: check whether CPU/Python marker parsing is a bottleneck.")
    print("No serial port is used in this test.")
    print("====================================================")

    one_frame = bytes([0]) * MAT_SIZE + VERIFICATION_SEQUENCE
    test_block = one_frame * 1000

    parsed_frames = 0
    bytes_scanned = 0

    wall_start = now()
    cpu_start = time.process_time()

    while now() - wall_start < duration:
        pos = 0

        while True:
            idx = test_block.find(VERIFICATION_SEQUENCE, pos)

            if idx < 0:
                break

            parsed_frames += 1
            pos = idx + VERIFICATION_WIDTH

        bytes_scanned += len(test_block)

    wall_time = now() - wall_start
    cpu_time = time.process_time() - cpu_start

    fps = parsed_frames / wall_time if wall_time > 0 else 0.0
    byte_rate = bytes_scanned / wall_time if wall_time > 0 else 0.0

    return {
        "name": "local_parser_benchmark",
        "duration": wall_time,
        "cpu_time": cpu_time,
        "cpu_percent_one_core": 100.0 * cpu_time / wall_time if wall_time > 0 else 0.0,
        "parsed_frames_per_sec": fps,
        "scan_byte_rate": byte_rate,
    }


# ============================================================
# Diagnosis
# ============================================================

def print_summary(byte_result, frame_result, bulk_result, parser_result, baud, target_fps, reference_fps, buffer_resize_ok):
    print()
    print()
    print("============================================================")
    print("                     FINAL DIAGNOSTIC SUMMARY")
    print("============================================================")
    print()

    print("========== Protocol ==========")
    print(f"Frame payload size:              {MAT_SIZE} bytes")
    print(f"Verification size:               {VERIFICATION_WIDTH} bytes")
    print(f"Total frame size:                {FRAME_SIZE} bytes")
    print(f"Verification sequence:           {list(VERIFICATION_SEQUENCE)}")
    print()

    print("========== Measured Results ==========")
    print(f"Byte drain estimated FPS:        {byte_result['estimated_fps']:.2f} Hz")
    print(f"Fixed frame read FPS:            {frame_result['fps']:.2f} Hz")
    print(f"Bulk parse marker FPS:           {bulk_result['fps']:.2f} Hz")
    print(f"Bulk parse FPS from byte rate:   {bulk_result['estimated_fps_from_bytes']:.2f} Hz")
    print(f"Local parser capacity:           {parser_result['parsed_frames_per_sec']:.0f} frames/s")
    print()

    print("========== Data Rates ==========")
    print(f"Byte drain rate:                 {byte_result['byte_rate']:.1f} B/s")
    print(f"Fixed frame read rate:           {frame_result['byte_rate']:.1f} B/s")
    print(f"Bulk parse read rate:            {bulk_result['byte_rate']:.1f} B/s")
    print()
    print(f"Measured bulk USB data need:     {bytes_to_mbps(bulk_result['byte_rate']):.4f} Mbps")
    print(f"Target {target_fps:.1f} Hz data need:           {bytes_to_mbps(byte_rate_from_fps(target_fps)):.4f} Mbps")
    print(f"Reference {reference_fps:.1f} Hz data need:      {bytes_to_mbps(byte_rate_from_fps(reference_fps)):.4f} Mbps")
    print()

    print("========== UART Equivalent ==========")
    print(f"Configured baud rate:            {baud}")
    print(f"Configured UART theoretical FPS: {(baud / 10.0) / FRAME_SIZE:.2f} Hz")
    print(f"Measured FPS UART equivalent:    {uart_equivalent_baud(bulk_result['fps']):.0f} baud")
    print(f"Target FPS UART equivalent:      {uart_equivalent_baud(target_fps):.0f} baud")
    print()
    print("Note: if this is USB CDC / virtual COM, the configured baud rate may not")
    print("represent the actual USB transfer rate.")
    print()

    print("========== Timing ==========")
    print("Fixed frame read:")
    print(f"  Average frame interval:        {frame_result['avg_frame_interval_ms']:.2f} ms")
    print(f"  P50 frame interval:            {frame_result['p50_frame_interval_ms']:.2f} ms")
    print(f"  P95 frame interval:            {frame_result['p95_frame_interval_ms']:.2f} ms")
    print(f"  P99 frame interval:            {frame_result['p99_frame_interval_ms']:.2f} ms")
    print(f"  Max frame interval:            {frame_result['max_frame_interval_ms']:.2f} ms")
    print(f"  Average serial read duration:  {frame_result['avg_read_ms']:.2f} ms")
    print(f"  Max serial read duration:      {frame_result['max_read_ms']:.2f} ms")
    print()
    print("Bulk parse:")
    print(f"  Average frame interval:        {bulk_result['avg_frame_interval_ms']:.2f} ms")
    print(f"  P50 frame interval:            {bulk_result['p50_frame_interval_ms']:.2f} ms")
    print(f"  P95 frame interval:            {bulk_result['p95_frame_interval_ms']:.2f} ms")
    print(f"  P99 frame interval:            {bulk_result['p99_frame_interval_ms']:.2f} ms")
    print(f"  Max frame interval:            {bulk_result['max_frame_interval_ms']:.2f} ms")
    print()

    print("========== Buffer / Backlog ==========")
    print(f"set_buffer_size worked:          {buffer_resize_ok}")
    print()
    print("Byte drain:")
    print(f"  Average in_waiting:            {byte_result['avg_in_waiting']:.1f} bytes")
    print(f"  Max in_waiting:                {byte_result['max_in_waiting']:.0f} bytes")
    print()
    print("Fixed frame read:")
    print(f"  Average in_waiting before:     {frame_result['avg_in_waiting_before']:.1f} bytes")
    print(f"  Max in_waiting before:         {frame_result['max_in_waiting_before']:.0f} bytes")
    print(f"  Average in_waiting after:      {frame_result['avg_in_waiting_after']:.1f} bytes")
    print(f"  Max in_waiting after:          {frame_result['max_in_waiting_after']:.0f} bytes")
    print()
    print("Bulk parse:")
    print(f"  Average in_waiting:            {bulk_result['avg_in_waiting']:.1f} bytes")
    print(f"  Max in_waiting:                {bulk_result['max_in_waiting']:.0f} bytes")
    print()

    print("========== Errors ==========")
    print(f"Fixed frame total frames:        {frame_result['total_frames']}")
    print(f"Fixed frame valid frames:        {frame_result['valid_frames']}")
    print(f"Fixed frame errors:              {frame_result['errors']}")
    print(f"Fixed frame short frames:        {frame_result['short_frames']}")
    print(f"Fixed frame timeouts:            {frame_result['timeouts']}")
    print()

    print("========== CPU Usage ==========")
    print("Approximate process CPU usage on one core:")
    print(f"  Byte drain:                    {byte_result['cpu_percent_one_core']:.1f}%")
    print(f"  Fixed frame read:              {frame_result['cpu_percent_one_core']:.1f}%")
    print(f"  Bulk parse:                    {bulk_result['cpu_percent_one_core']:.1f}%")
    print(f"  Local parser benchmark:        {parser_result['cpu_percent_one_core']:.1f}%")
    print()

    print("========== Bottleneck Interpretation ==========")

    byte_fps = byte_result["estimated_fps"]
    frame_fps = frame_result["fps"]
    bulk_fps = bulk_result["fps"]
    parser_fps = parser_result["parsed_frames_per_sec"]

    large_backlog = (
        frame_result["max_in_waiting_after"] > FRAME_SIZE * 5
        or bulk_result["max_in_waiting"] > FRAME_SIZE * 5
        or byte_result["max_in_waiting"] > FRAME_SIZE * 5
    )

    has_errors = frame_result["errors"] > 0 or frame_result["short_frames"] > 0 or frame_result["timeouts"] > 0

    if parser_fps > 1000 * max(bulk_fps, 1):
        print("1. Python marker parsing is NOT the bottleneck.")
        print("   Local parser capacity is much higher than the measured serial FPS.")
    else:
        print("1. Python parsing may be a bottleneck.")
        print("   The local parser benchmark is not much faster than the measured stream.")

    print()

    if byte_fps > frame_fps * 1.5 and bulk_fps > frame_fps * 1.5:
        print("2. Per-frame serial reading is likely a bottleneck.")
        print("   Byte drain and bulk parse are much faster than fixed-size frame reads.")
        print("   Use the bulk parser method in your application.")
    elif abs(bulk_fps - frame_fps) / max(bulk_fps, frame_fps, 1) < 0.2:
        print("2. Per-frame serial reading is probably NOT the main bottleneck.")
        print("   Fixed frame read and bulk parse are similar.")
    else:
        print("2. Per-frame reading may contribute, but it is not the only factor.")

    print()

    if large_backlog:
        print("3. Receive buffer backlog was detected.")
        print("   The host PC or Python program may not be consuming data fast enough.")
        print("   This can cause the firmware USB write to block and reduce frame rate.")
    else:
        print("3. No large receive backlog was detected.")
        print("   The Python script appears to be consuming data as it arrives.")

    print()

    if has_errors:
        print("4. Transmission errors, short frames, or timeouts were detected.")
        print("   Check USB cable, USB hub, USB power saving, driver stability, and frame alignment.")
    else:
        print("4. No frame errors or timeouts were detected in the fixed frame test.")

    print()

    if bulk_fps < reference_fps * 0.7:
        print("5. This host is much slower than the reference host.")
        print("   Since the mat is the same, likely host-side causes are:")
        print("   - USB controller or driver behavior")
        print("   - USB power saving / selective suspend")
        print("   - USB hub or cable issue")
        print("   - Slow application consuming the serial stream")
        print("   - Firmware USB write blocking because the host accepts data slowly")
    elif bulk_fps < reference_fps * 0.9:
        print("5. This host is moderately slower than the reference host.")
        print("   Host-side USB/driver/application behavior may be affecting throughput.")
    else:
        print("5. This host is close to the reference frame rate.")

    print()


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Diagnose pressure mat frame-rate bottlenecks caused by different host computers."
    )

    parser.add_argument(
        "--port",
        required=True,
        help="Serial port, e.g. COM8 on Windows or /dev/ttyACM0 on Linux/Raspberry Pi",
    )

    parser.add_argument(
        "--baud",
        type=int,
        default=115200,
        help="Configured serial baud rate. For USB CDC this may not affect real transfer speed.",
    )

    parser.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="Duration of each serial test phase in seconds.",
    )

    parser.add_argument(
        "--parser-duration",
        type=float,
        default=2.0,
        help="Duration of local parser benchmark in seconds.",
    )

    parser.add_argument(
        "--chunk-size",
        type=int,
        default=65536,
        help="Read chunk size for byte drain and bulk parse tests.",
    )

    parser.add_argument(
        "--target-fps",
        type=float,
        default=100.0,
        help="Target FPS for comparison.",
    )

    parser.add_argument(
        "--reference-fps",
        type=float,
        default=46.0,
        help="Known-good FPS from your reference computer.",
    )

    args = parser.parse_args()

    print_host_and_port_info(args.port, args.baud)

    ser, buffer_resize_ok = open_serial(
        port=args.port,
        baud=args.baud,
        timeout=0.02,
    )

    try:
        print(f"Sending command: {START_READING_COMMAND}")
        send_start_command(ser)
        time.sleep(1.0)

        byte_result = run_byte_drain_test(
            ser=ser,
            duration=args.duration,
            chunk_size=args.chunk_size,
        )

        frame_result = run_frame_read_test(
            ser=ser,
            duration=args.duration,
        )

        bulk_result = run_bulk_parse_test(
            ser=ser,
            duration=args.duration,
            chunk_size=args.chunk_size,
        )

    finally:
        ser.close()

    parser_result = run_local_parser_benchmark(
        duration=args.parser_duration,
    )

    print_summary(
        byte_result=byte_result,
        frame_result=frame_result,
        bulk_result=bulk_result,
        parser_result=parser_result,
        baud=args.baud,
        target_fps=args.target_fps,
        reference_fps=args.reference_fps,
        buffer_resize_ok=buffer_resize_ok,
    )


if __name__ == "__main__":
    main()
    