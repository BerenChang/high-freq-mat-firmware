#!/usr/bin/env python3
"""
Test start_reading -> stop_reading -> start_reading for the pressure-mat firmware.

Expected protocol:
- Commands are ASCII lines:
    start_reading\n
    stop_reading\n
- Each transmitted frame contains:
    1568 pressure bytes + FF FE FE FF

The script:
1. Places the firmware in its stopped/idle state.
2. Starts streaming and validates complete frames.
3. Sends stop_reading and waits for the current frame/transmission to finish.
4. Verifies that the serial stream remains quiet.
5. Sends start_reading again and validates complete frames.
6. Sends stop_reading before exiting.

Install dependency:
    python -m pip install pyserial

Example:
    python test_start_stop_restart.py --port COM3
    python test_start_stop_restart.py --port /dev/ttyACM0
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass
from typing import List

import serial
from serial import SerialException


PAYLOAD_SIZE = 28 * 56
MARKER = bytes((0xFF, 0xFE, 0xFE, 0xFF))
FRAME_SIZE = PAYLOAD_SIZE + len(MARKER)

START_COMMAND = "start_reading"
STOP_COMMAND = "stop_reading"


@dataclass
class PhaseResult:
    name: str
    frames: List[bytes]
    bytes_received: int
    elapsed_s: float
    frame_timestamps: List[float]
    resyncs: int
    discarded_bytes: int
    pending_bytes: int

    @property
    def average_fps(self) -> float:
        if len(self.frame_timestamps) >= 2:
            duration = self.frame_timestamps[-1] - self.frame_timestamps[0]
            if duration > 0:
                return (len(self.frame_timestamps) - 1) / duration

        if self.elapsed_s > 0:
            return len(self.frames) / self.elapsed_s

        return 0.0


class FrameParser:
    """
    Parse frames whose verification marker is transmitted after the payload.

    The first marker is used only to establish alignment because the script may
    begin reading in the middle of a frame. Once aligned, the next marker must
    occur exactly PAYLOAD_SIZE bytes later.
    """

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.synced = False
        self.resyncs = 0
        self.discarded_bytes = 0

    def feed(self, data: bytes) -> List[bytes]:
        if data:
            self.buffer.extend(data)

        frames: List[bytes] = []
        marker_size = len(MARKER)

        while True:
            if not self.synced:
                marker_index = self.buffer.find(MARKER)

                if marker_index < 0:
                    # Retain only enough trailing bytes to detect a marker that
                    # is split across two serial reads.
                    keep = marker_size - 1
                    if len(self.buffer) > keep:
                        remove_count = len(self.buffer) - keep
                        del self.buffer[:remove_count]
                        self.discarded_bytes += remove_count
                    break

                # This marker terminates a possibly partial initial frame.
                remove_count = marker_index + marker_size
                del self.buffer[:remove_count]
                self.discarded_bytes += remove_count
                self.synced = True
                continue

            expected_size = PAYLOAD_SIZE + marker_size
            if len(self.buffer) < expected_size:
                break

            if self.buffer[PAYLOAD_SIZE:expected_size] == MARKER:
                payload = bytes(self.buffer[:PAYLOAD_SIZE])
                del self.buffer[:expected_size]
                frames.append(payload)
                continue

            # Alignment was lost. Find another marker and use it as a new
            # candidate frame boundary.
            marker_index = self.buffer.find(MARKER)
            self.resyncs += 1

            if marker_index < 0:
                keep = marker_size - 1
                remove_count = max(0, len(self.buffer) - keep)
                if remove_count:
                    del self.buffer[:remove_count]
                    self.discarded_bytes += remove_count
                self.synced = False
                break

            remove_count = marker_index + marker_size
            del self.buffer[:remove_count]
            self.discarded_bytes += remove_count
            self.synced = True

        return frames


def send_command(
    ser: serial.Serial,
    command: str,
    line_ending: bytes,
) -> None:
    packet = command.encode("ascii") + line_ending
    ser.write(packet)
    ser.flush()
    print(f"[TX] {command}")


def read_serial_chunk(ser: serial.Serial) -> bytes:
    waiting = ser.in_waiting
    return ser.read(waiting if waiting > 0 else 1)


def drain_raw_until_quiet(
    ser: serial.Serial,
    quiet_gap_s: float,
    maximum_wait_s: float,
) -> tuple[bool, int]:
    """Drain arbitrary data until no bytes arrive for quiet_gap_s."""
    start = time.monotonic()
    last_data_time = start
    total_bytes = 0

    while time.monotonic() - start < maximum_wait_s:
        data = read_serial_chunk(ser)
        now = time.monotonic()

        if data:
            total_bytes += len(data)
            last_data_time = now

        if now - last_data_time >= quiet_gap_s:
            return True, total_bytes

    return False, total_bytes


def collect_frames(
    ser: serial.Serial,
    parser: FrameParser,
    target_frames: int,
    timeout_s: float,
    phase_name: str,
) -> PhaseResult:
    print(
        f"[{phase_name}] Waiting for {target_frames} valid frames "
        f"(timeout {timeout_s:.1f} s)..."
    )

    start = time.monotonic()
    frames: List[bytes] = []
    timestamps: List[float] = []
    total_bytes = 0

    while len(frames) < target_frames:
        if time.monotonic() - start >= timeout_s:
            break

        data = read_serial_chunk(ser)
        if not data:
            continue

        total_bytes += len(data)
        parsed = parser.feed(data)

        if parsed:
            timestamp = time.monotonic()
            for payload in parsed:
                frames.append(payload)
                timestamps.append(timestamp)
                if len(frames) >= target_frames:
                    break

    elapsed = time.monotonic() - start

    return PhaseResult(
        name=phase_name,
        frames=frames,
        bytes_received=total_bytes,
        elapsed_s=elapsed,
        frame_timestamps=timestamps,
        resyncs=parser.resyncs,
        discarded_bytes=parser.discarded_bytes,
        pending_bytes=len(parser.buffer),
    )


def finish_after_stop(
    ser: serial.Serial,
    parser: FrameParser,
    quiet_gap_s: float,
    maximum_wait_s: float,
) -> tuple[bool, int, int]:
    """
    Continue reading after stop_reading.

    A complete frame that was already being acquired/transmitted is allowed.
    Success means the stream eventually remains silent for quiet_gap_s.
    """
    start = time.monotonic()
    last_data_time = start
    total_bytes = 0
    complete_tail_frames = 0

    while time.monotonic() - start < maximum_wait_s:
        data = read_serial_chunk(ser)
        now = time.monotonic()

        if data:
            total_bytes += len(data)
            last_data_time = now
            complete_tail_frames += len(parser.feed(data))

        if now - last_data_time >= quiet_gap_s:
            return True, total_bytes, complete_tail_frames

    return False, total_bytes, complete_tail_frames


def verify_quiet(ser: serial.Serial, duration_s: float) -> int:
    """Return the number of bytes unexpectedly received during idle."""
    start = time.monotonic()
    unexpected_bytes = 0

    while time.monotonic() - start < duration_s:
        data = read_serial_chunk(ser)
        unexpected_bytes += len(data)

    return unexpected_bytes


def print_phase_summary(result: PhaseResult) -> None:
    print(f"\n========== {result.name} ==========")
    print(f"Valid frames:       {len(result.frames)}")
    print(f"Bytes received:     {result.bytes_received}")
    print(f"Elapsed time:       {result.elapsed_s:.3f} s")
    print(f"Approx. frame rate: {result.average_fps:.2f} Hz")
    print(f"Parser resyncs:     {result.resyncs}")
    print(f"Discarded bytes:    {result.discarded_bytes}")
    print(f"Pending bytes:      {result.pending_bytes}")

    if result.frames:
        minimum = min(min(frame) for frame in result.frames)
        maximum = max(max(frame) for frame in result.frames)
        means = [statistics.fmean(frame) for frame in result.frames]
        print(f"ADC value range:    {minimum}..{maximum}")
        print(f"Mean ADC value:     {statistics.fmean(means):.2f}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test pressure-mat start, stop, and restart behavior."
    )
    parser.add_argument(
        "--port",
        required=True,
        help="Serial port, for example COM3 or /dev/ttyACM0",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=115200,
        help="Serial baud setting passed to PySerial (default: 115200)",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=20,
        help="Valid frames required in each streaming phase (default: 20)",
    )
    parser.add_argument(
        "--phase-timeout",
        type=float,
        default=10.0,
        help="Maximum time for each streaming phase (default: 10 s)",
    )
    parser.add_argument(
        "--stop-timeout",
        type=float,
        default=4.0,
        help="Maximum time for streaming to stop (default: 4 s)",
    )
    parser.add_argument(
        "--quiet-gap",
        type=float,
        default=0.6,
        help="Continuous silence needed to consider the mat stopped (default: 0.6 s)",
    )
    parser.add_argument(
        "--quiet-check",
        type=float,
        default=1.5,
        help="Additional idle-monitoring time after stop (default: 1.5 s)",
    )
    parser.add_argument(
        "--startup-delay",
        type=float,
        default=1.0,
        help="Delay after opening the port (default: 1 s)",
    )
    parser.add_argument(
        "--crlf",
        action="store_true",
        help="Terminate commands with CRLF instead of LF",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.frames < 1:
        print("ERROR: --frames must be at least 1.", file=sys.stderr)
        return 2

    line_ending = b"\r\n" if args.crlf else b"\n"
    ser: serial.Serial | None = None
    failures: List[str] = []

    try:
        print(f"Opening {args.port} at {args.baud}...")
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            timeout=0.05,
            write_timeout=1.0,
        )

        time.sleep(args.startup_delay)
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        # Establish a known idle state. This also handles a board that was
        # already streaming when the serial port was opened.
        print("\n[SETUP] Requesting idle state...")
        send_command(ser, STOP_COMMAND, line_ending)
        setup_quiet, setup_bytes = drain_raw_until_quiet(
            ser,
            quiet_gap_s=args.quiet_gap,
            maximum_wait_s=args.stop_timeout,
        )
        print(f"[SETUP] Drained {setup_bytes} byte(s).")

        if not setup_quiet:
            failures.append(
                "The board did not become quiet during initial setup. "
                "stop_reading may not be implemented or recognized."
            )
            raise RuntimeError(failures[-1])

        ser.reset_input_buffer()

        # First streaming phase.
        parser1 = FrameParser()
        send_command(ser, START_COMMAND, line_ending)
        first = collect_frames(
            ser,
            parser=parser1,
            target_frames=args.frames,
            timeout_s=args.phase_timeout,
            phase_name="FIRST START",
        )
        print_phase_summary(first)

        if len(first.frames) < args.frames:
            failures.append(
                f"First start produced only {len(first.frames)}/{args.frames} "
                "valid frames."
            )

        # Stop and allow any frame already in progress to finish.
        print("\n[STOP] Sending stop_reading...")
        send_command(ser, STOP_COMMAND, line_ending)
        became_quiet, tail_bytes, tail_frames = finish_after_stop(
            ser,
            parser=parser1,
            quiet_gap_s=args.quiet_gap,
            maximum_wait_s=args.stop_timeout,
        )

        print(f"[STOP] Tail bytes received:   {tail_bytes}")
        print(f"[STOP] Tail complete frames:  {tail_frames}")
        print(f"[STOP] Pending parser bytes:  {len(parser1.buffer)}")
        print(f"[STOP] Became quiet:          {became_quiet}")

        if not became_quiet:
            failures.append(
                "Data continued after stop_reading; the board never reached "
                "the required quiet interval."
            )

        if parser1.buffer:
            failures.append(
                f"Streaming stopped with {len(parser1.buffer)} byte(s) of an "
                "incomplete or unexpected frame."
            )

        print(
            f"\n[IDLE] Checking for unexpected data for "
            f"{args.quiet_check:.1f} s..."
        )
        unexpected_idle_bytes = verify_quiet(ser, args.quiet_check)
        print(f"[IDLE] Unexpected bytes: {unexpected_idle_bytes}")

        if unexpected_idle_bytes:
            failures.append(
                f"Received {unexpected_idle_bytes} byte(s) while the firmware "
                "should have been stopped."
            )

        # Restart from a clean host-side parser.
        ser.reset_input_buffer()
        parser2 = FrameParser()

        print("\n[RESTART] Sending start_reading again...")
        send_command(ser, START_COMMAND, line_ending)
        second = collect_frames(
            ser,
            parser=parser2,
            target_frames=args.frames,
            timeout_s=args.phase_timeout,
            phase_name="RESTART",
        )
        print_phase_summary(second)

        if len(second.frames) < args.frames:
            failures.append(
                f"Restart produced only {len(second.frames)}/{args.frames} "
                "valid frames."
            )

        if parser2.resyncs:
            failures.append(
                f"The restarted stream required {parser2.resyncs} parser "
                "resynchronization(s)."
            )

    except (SerialException, OSError, RuntimeError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        if not failures:
            failures.append(str(exc))

    finally:
        if ser is not None and ser.is_open:
            try:
                print("\n[CLEANUP] Sending stop_reading...")
                send_command(ser, STOP_COMMAND, line_ending)
                drain_raw_until_quiet(
                    ser,
                    quiet_gap_s=args.quiet_gap,
                    maximum_wait_s=args.stop_timeout,
                )
            except (SerialException, OSError):
                pass
            ser.close()

    print("\n================ RESULT ================")
    if failures:
        print("FAIL")
        for index, failure in enumerate(failures, start=1):
            print(f"{index}. {failure}")
        return 1

    print("PASS")
    print(
        "The firmware streamed valid frames, stopped, remained idle, "
        "and successfully restarted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
