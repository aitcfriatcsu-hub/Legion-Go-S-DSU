#!/usr/bin/env python3
"""
legion-dsu.py - tiny DSU (cemuhook) motion server for the Legion Go S on SteamOS.

Reads the Steam-Deck-format HID reports that InputPlumber exposes on a virtual
"Legion Go S Controller" (USB ID 28de:12ff) and serves the gyro/accel over the
DSU protocol on 127.0.0.1:26760 for Eden, Ryubing/Ryujinx, Cemu, etc.

Report layout and scaling follow SteamDeckGyroDSU (kmicki):
  bytes  0-3   marker 01 00 09 40
  bytes  4-7   packet counter (u32)
  bytes 24-29  accel  3 x int16   (16384 = 1 g)
  bytes 30-35  gyro   3 x int16   (16 = 1 deg/s)

Usage:
  python3 legion-dsu.py --probe          # print live raw values (tilt the device!)
  python3 legion-dsu.py                  # run the DSU server
Options: see --help
"""
import argparse
import glob
import os
import select
import socket
import struct
import sys
import threading
import time
import zlib

MARKER = bytes([0x01, 0x00, 0x09, 0x40])
FRAME_LEN = 64
ACCEL_1G = 16384.0
GYRO_1DPS = 16.0

VERSION_TYPE = 0x100000
INFO_TYPE = 0x100001
DATA_TYPE = 0x100002


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def find_hidraw(vid, pid):
    for uevent in sorted(glob.glob("/sys/class/hidraw/hidraw*/device/uevent")):
        try:
            txt = open(uevent).read()
        except OSError:
            continue
        for line in txt.splitlines():
            if line.startswith("HID_ID="):
                parts = line.split("=", 1)[1].split(":")
                if len(parts) == 3 and int(parts[1], 16) == vid and int(parts[2], 16) == pid:
                    return "/dev/" + uevent.split("/")[4]
    return None


class Frame:
    __slots__ = ("inc", "accel", "gyro", "t")

    def __init__(self, inc, accel, gyro):
        self.inc = inc
        self.accel = accel  # raw int16 x3 (right-left, top-bottom, front-back)
        self.gyro = gyro    # raw int16 x3
        self.t = time.monotonic()


def parse_frame(b):
    inc = struct.unpack_from("<I", b, 4)[0]
    accel = struct.unpack_from("<hhh", b, 24)
    gyro = struct.unpack_from("<hhh", b, 30)
    return Frame(inc, accel, gyro)


class Reader(threading.Thread):
    def __init__(self, args):
        super().__init__(daemon=True)
        self.args = args
        self.cv = threading.Condition()
        self.latest = None
        self.seq = 0
        self.connected = False

    def open_dev(self):
        while True:
            path = self.args.device or find_hidraw(self.args.vid, self.args.pid)
            if path:
                try:
                    fd = os.open(path, os.O_RDONLY)
                    log("reading", path)
                    return fd
                except OSError as e:
                    log("cannot open %s: %s (need permission? see udev rule)" % (path, e))
            else:
                log("controller %04x:%04x not found, retrying..." % (self.args.vid, self.args.pid))
            time.sleep(2)

    def run(self):
        while True:
            fd = self.open_dev()
            self.connected = True
            buf = b""
            try:
                while True:
                    r, _, _ = select.select([fd], [], [], 3.0)
                    if not r:
                        continue
                    chunk = os.read(fd, 256)
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= FRAME_LEN:
                        if buf[:4] == MARKER:
                            fr = parse_frame(buf[:FRAME_LEN])
                            buf = buf[FRAME_LEN:]
                            with self.cv:
                                self.latest = fr
                                self.seq += 1
                                self.cv.notify_all()
                        else:
                            buf = buf[1:]
            except OSError as e:
                log("read error:", e)
            finally:
                self.connected = False
                try:
                    os.close(fd)
                except OSError:
                    pass
            log("device went away (InputPlumber restart?), reconnecting...")
            time.sleep(1)

    def wait_new(self, last_seq, timeout=1.0):
        with self.cv:
            if self.seq == last_seq:
                self.cv.wait(timeout)
            return self.seq, self.latest


def build_packet(event_type, payload, server_id):
    # header: magic(4) ver(2) len(2) crc(4) id(4) event(4) -> 20 bytes; len counts event type + payload
    body = struct.pack("<I", event_type) + payload
    hdr = b"DSUS" + struct.pack("<HHII", 1001, len(body), 0, server_id)
    pkt = bytearray(hdr + body)
    crc = zlib.crc32(bytes(pkt)) & 0xFFFFFFFF
    struct.pack_into("<I", pkt, 8, crc)
    return bytes(pkt)


def shared_response(slot, connected):
    # slot, state(2=connected), model(2=full gyro), connection(1=USB), mac(6), battery(0)
    state = 2 if connected else 0
    model = 2 if connected else 0
    conn = 1 if connected else 0
    return struct.pack("<BBBB6sB", slot, state, model, conn, b"\x00" * 6, 0)


class Server:
    def __init__(self, args, reader):
        self.args = args
        self.reader = reader
        self.id = int.from_bytes(os.urandom(4), "little")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((args.bind, args.port))
        self.clients = {}  # addr -> last_seen
        self.lock = threading.Lock()
        self.pkt_no = 0
        self.t0 = time.monotonic()
        log("DSU server listening on %s:%d" % (args.bind, args.port))

    def motion(self, fr):
        a = self.args
        rl, tb, fb = fr.accel
        grl, gtb, gfb = fr.gyro

        def dz(v):
            return 0 if abs(v) < a.deadzone else v

        grl, gtb, gfb = dz(grl), dz(gtb), dz(gfb)
        accx = -rl / ACCEL_1G
        accy = -fb / ACCEL_1G
        accz = tb / ACCEL_1G
        pitch = grl / GYRO_1DPS
        yaw = -gfb / GYRO_1DPS
        roll = gtb / GYRO_1DPS
        s = a.gyro_scale
        pitch *= s * (-1 if a.invert_pitch else 1)
        yaw *= s * (-1 if a.invert_yaw else 1)
        roll *= s * (-1 if a.invert_roll else 1)
        return accx, accy, accz, pitch, yaw, roll

    def data_packet(self, fr):
        connected = self.reader.connected and fr is not None
        self.pkt_no = (self.pkt_no + 1) & 0xFFFFFFFF
        ts = int((time.monotonic() - self.t0) * 1e6)
        p = shared_response(self.args.slot, connected) + struct.pack("<B", 1 if connected else 0)
        p += struct.pack("<I", self.pkt_no)
        # buttons1, buttons2, home, touch
        p += bytes([0, 0, 0, 0])
        # sticks neutral (128), dpad/face/shoulder analog zeros
        p += bytes([128, 128, 128, 128])
        p += bytes(12)
        # two touch slots (6 bytes each), unused
        p += bytes(12)
        if fr is not None:
            ax, ay, az, pi, ya, ro = self.motion(fr)
        else:
            ax = ay = az = pi = ya = ro = 0.0
        p += struct.pack("<Q6f", ts, ax, ay, az, pi, ya, ro)
        return build_packet(DATA_TYPE, p, self.id)

    def recv_loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(1024)
            except OSError:
                return
            if len(data) < 20 or data[:4] != b"DSUC":
                continue
            ev = struct.unpack_from("<I", data, 16)[0]
            if ev == VERSION_TYPE:
                self.sock.sendto(build_packet(VERSION_TYPE, struct.pack("<H", 1001), self.id), addr)
            elif ev == INFO_TYPE:
                if len(data) >= 24:
                    cnt = struct.unpack_from("<i", data, 20)[0]
                    slots = data[24:24 + max(0, min(cnt, 4))]
                    for s in slots:
                        sr = shared_response(s, s == self.args.slot and self.reader.connected)
                        self.sock.sendto(build_packet(INFO_TYPE, sr + b"\x00", self.id), addr)
            elif ev == DATA_TYPE:
                with self.lock:
                    new = addr not in self.clients
                    self.clients[addr] = time.monotonic()
                if new:
                    log("client subscribed:", addr)

    def send_loop(self):
        last_seq = -1
        while True:
            seq, fr = self.reader.wait_new(last_seq, timeout=0.5)
            now = time.monotonic()
            with self.lock:
                for a in [a for a, t in self.clients.items() if now - t > 5.0]:
                    del self.clients[a]
                    log("client timed out:", a)
                targets = list(self.clients)
            if fr is None and not targets:
                continue
            if seq == last_seq and fr is not None:
                # no new frame within timeout: still send keepalive with last data
                pass
            last_seq = seq
            if not targets:
                continue
            pkt = self.data_packet(fr)
            for t in targets:
                try:
                    self.sock.sendto(pkt, t)
                except OSError:
                    pass

    def run(self):
        threading.Thread(target=self.recv_loop, daemon=True).start()
        self.send_loop()


def probe(reader):
    log("Probe mode. Tilt and rotate the device. Ctrl+C to stop.")
    mn_a = [0] * 3
    mx_a = [0] * 3
    mn_g = [0] * 3
    mx_g = [0] * 3
    last = -1
    t_print = 0
    while True:
        seq, fr = reader.wait_new(last, timeout=1.0)
        if fr is None:
            if time.time() - t_print > 2:
                print("waiting for controller reports...", flush=True)
                t_print = time.time()
            continue
        last = seq
        for i in range(3):
            mn_a[i] = min(mn_a[i], fr.accel[i]); mx_a[i] = max(mx_a[i], fr.accel[i])
            mn_g[i] = min(mn_g[i], fr.gyro[i]); mx_g[i] = max(mx_g[i], fr.gyro[i])
        if time.time() - t_print > 0.2:
            t_print = time.time()
            print("\rinc=%-9d accel(RL,TB,FB)=%6d %6d %6d  gyro=%6d %6d %6d   "
                  % ((fr.inc,) + fr.accel + fr.gyro), end="", flush=True)
        # periodic range summary
        if seq % 1500 == 0:
            print("\n  accel min/max:", mn_a, mx_a, " gyro min/max:", mn_g, mx_g, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true", help="print live raw accel/gyro values and exit with Ctrl+C")
    ap.add_argument("--device", help="explicit /dev/hidrawN (default: auto-find by VID:PID)")
    ap.add_argument("--vid", type=lambda x: int(x, 16), default=0x28DE, help="hex vendor id (default 28de)")
    ap.add_argument("--pid", type=lambda x: int(x, 16), default=0x12FF, help="hex product id (default 12ff)")
    ap.add_argument("--bind", default="127.0.0.1", help="UDP bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=26760)
    ap.add_argument("--slot", type=int, default=0, help="DSU controller slot 0-3")
    ap.add_argument("--deadzone", type=int, default=8, help="gyro deadzone in raw units (16 = 1 deg/s)")
    ap.add_argument("--gyro-scale", type=float, default=1.0, help="multiply gyro rates (sensitivity)")
    ap.add_argument("--invert-pitch", action="store_true")
    ap.add_argument("--invert-yaw", action="store_true")
    ap.add_argument("--invert-roll", action="store_true")
    args = ap.parse_args()

    reader = Reader(args)
    reader.start()
    if args.probe:
        try:
            probe(reader)
        except KeyboardInterrupt:
            print()
        return
    srv = Server(args, reader)
    try:
        srv.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
