"""
DVM Nexus - Bidirectional Voice <-> Meshtastic Bridge (STT / TTS)
Translates incoming DMR/P25 voice frames into Meshtastic text via local Whisper STT,
and converts incoming Meshtastic text messages to synthesized voice via Piper TTS,
transmitting frames back to dvmbridge over UDP PCM.
"""

import os
import io
import wave
import time
import socket
import select
import struct
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from dvm_nexus.core.database import get_db_connection, log_audit

logger = logging.getLogger("dvm_nexus.meshtastic.voice_gateway")


class VoiceBridgeGateway:
    """Manages audio UDP streaming, offline STT (Whisper), and offline TTS (Piper)."""

    def __init__(self, meshtastic_manager=None):
        self._manager = meshtastic_manager
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # State tracking for UI
        self._state = "idle"  # idle, listening, transcribing, transmitting
        self._last_active_time = time.time()
        self._last_tts_time = 0.0
        self._current_src_id = 0
        self._current_dst_id = 0

        # Lazy-loaded models
        self._whisper_model = None
        self._whisper_model_name = None

    def set_manager(self, manager):
        self._manager = manager

    def get_config(self) -> Dict[str, str]:
        """Load voice bridge settings from database."""
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM voice_bridge_config")
            return dict(cur.fetchall())
        finally:
            conn.close()

    def save_config(self, **kwargs):
        """Update voice bridge settings."""
        conn = get_db_connection()
        try:
            with conn:
                for k, v in kwargs.items():
                    conn.execute("INSERT OR REPLACE INTO voice_bridge_config (key, value) VALUES (?, ?)", (k, str(v)))
            log_audit("VOICE_BRIDGE", "SAVE_CONFIG", f"Updated voice bridge settings: {list(kwargs.keys())}")
        finally:
            conn.close()

    def get_status(self) -> Dict[str, Any]:
        """Return runtime status for Web UI and live status pills."""
        cfg = self.get_config()
        is_echo_locked = (time.time() - self._last_tts_time) < float(cfg.get("echo_lockout_sec", "3.0"))
        return {
            "enabled": cfg.get("enabled", "0") == "1",
            "stt_enabled": cfg.get("stt_enabled", "1") == "1",
            "tts_enabled": cfg.get("tts_enabled", "1") == "1",
            "state": self._state,
            "is_echo_locked": is_echo_locked,
            "running": self._running,
            "voice_talkgroup": int(cfg.get("voice_talkgroup", "99")),
            "mesh_channel": int(cfg.get("mesh_channel", "0")),
            "whisper_model": cfg.get("whisper_model", "tiny.en"),
            "current_src_id": self._current_src_id,
            "current_dst_id": self._current_dst_id,
        }

    def get_recent_logs(self, limit: int = 40) -> List[Dict[str, Any]]:
        """Retrieve recent voice bridge activity logs."""
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, timestamp, direction, protocol, radio_src_id, radio_dest_id,
                       mesh_node_id, mesh_channel, text, duration_sec, status
                FROM voice_bridge_logs ORDER BY id DESC LIMIT ?
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def log_activity(self, direction: str, protocol: str, text: str,
                     radio_src_id: int = 0, radio_dest_id: int = 0,
                     mesh_node_id: str = "^all", mesh_channel: int = 0,
                     duration_sec: float = 0.0, status: str = "completed"):
        """Record voice bridge event in database."""
        conn = get_db_connection()
        now_str = datetime.now(timezone.utc).isoformat()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO voice_bridge_logs
                    (timestamp, direction, protocol, radio_src_id, radio_dest_id, mesh_node_id, mesh_channel, text, duration_sec, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (now_str, direction, protocol, radio_src_id, radio_dest_id, mesh_node_id, mesh_channel, text, duration_sec, status))
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # Service Lifecycle (UDP Audio Worker)
    # --------------------------------------------------------------------------
    def start(self):
        """Start UDP listener thread for dvmbridge audio."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._udp_listener_loop, daemon=True, name="VoiceBridgeUDP")
            self._thread.start()
            logger.info("VoiceBridge UDP audio listener started.")

    def stop(self):
        """Stop UDP listener thread."""
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._state = "idle"
        logger.info("VoiceBridge UDP audio listener stopped.")

    def _udp_listener_loop(self):
        """Listen on local UDP port for PCM audio packets from dvmbridge."""
        cfg = self.get_config()
        rx_port = int(cfg.get("udp_rx_port", "34001"))
        rx_host = cfg.get("udp_host", "127.0.0.1")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((rx_host, rx_port))
            sock.setblocking(False)
            logger.info(f"VoiceBridge bound to UDP {rx_host}:{rx_port}")
        except Exception as e:
            logger.error(f"Failed to bind VoiceBridge UDP socket: {e}")
            self._running = False
            return

        pcm_buffer = bytearray()
        call_active = False
        last_packet_time = 0.0
        silence_timeout = 0.6  # 600ms hangover to detect End-of-Transmission (EOT)
        src_id = 0
        dst_id = 0

        while self._running:
            try:
                ready = select.select([sock], [], [], 0.1)
                now = time.time()

                if ready[0]:
                    data, _ = sock.recvfrom(2048)
                    if not data:
                        continue

                    # Check Echo-Lockout Cooldown (prevent loopback while TTS is transmitting)
                    cfg = self.get_config()
                    echo_lock = float(cfg.get("echo_lockout_sec", "3.0"))
                    if (now - self._last_tts_time) < echo_lock:
                        continue

                    # Parse dvmbridge audio packet
                    # dvmbridge metadata format (if enabled):
                    # Bytes 0-3: Tag/Magic or Seq, or direct 16-bit PCM samples
                    audio_payload = data
                    if len(data) >= 12 and data[:4] == b"DVMB":
                        # Custom metadata header: [4s: 'DVMB'][4B: srcId][4B: dstId]
                        src_id, dst_id = struct.unpack("!II", data[4:12])
                        audio_payload = data[12:]
                    elif len(data) >= 12 and data[:4] == b"USRP":
                        # USRP header is 32 bytes
                        if len(data) > 32:
                            audio_payload = data[32:]

                    if len(audio_payload) > 0:
                        pcm_buffer.extend(audio_payload)
                        last_packet_time = now
                        if not call_active:
                            call_active = True
                            self._state = "listening"
                            self._current_src_id = src_id
                            self._current_dst_id = dst_id

                # Check for End of Transmission (silence / hangover exceeded)
                if call_active and (now - last_packet_time) > silence_timeout:
                    call_active = False
                    self._state = "transcribing"
                    audio_to_process = bytes(pcm_buffer)
                    pcm_buffer.clear()

                    # Transcribe and forward in background thread to avoid blocking socket
                    threading.Thread(
                        target=self._process_rx_audio,
                        args=(audio_to_process, src_id, dst_id),
                        daemon=True
                    ).start()

            except Exception as e:
                logger.debug(f"VoiceBridge UDP loop error: {e}")
                time.sleep(0.05)

        sock.close()

    # --------------------------------------------------------------------------
    # STT: Radio Voice -> Meshtastic Text
    # --------------------------------------------------------------------------
    def _process_rx_audio(self, pcm_bytes: bytes, src_id: int, dst_id: int):
        """Transcribe buffered 8kHz 16-bit PCM audio and route to Meshtastic."""
        try:
            cfg = self.get_config()
            if cfg.get("enabled", "0") != "1" or cfg.get("stt_enabled", "1") != "1":
                self._state = "idle"
                return

            filter_tg = int(cfg.get("voice_talkgroup", "99"))
            # If dst_id is present and does not match our voice talkgroup, ignore
            if dst_id > 0 and filter_tg > 0 and dst_id != filter_tg:
                logger.debug(f"VoiceBridge ignoring call for TG {dst_id} (Target is {filter_tg})")
                self._state = "idle"
                return

            duration = len(pcm_bytes) / (8000 * 2)  # 8000 Hz, 16-bit mono = 16000 bytes/sec
            if duration < 0.5:
                # Ignore blips under 500ms
                self._state = "idle"
                return

            text = self.transcribe_audio(pcm_bytes)
            if not text or not text.strip():
                self._state = "idle"
                return

            clean_text = text.strip()
            src_label = f"Radio {src_id}" if src_id > 0 else "Radio"
            mesh_msg = f"🎙️ [{src_label} -> TG {dst_id or filter_tg}]: {clean_text}"

            # Truncate to Meshtastic payload limit (~200 chars)
            if len(mesh_msg) > 200:
                mesh_msg = mesh_msg[:197] + "..."

            mesh_channel = int(cfg.get("mesh_channel", "0"))

            # Send via Meshtastic Manager
            if self._manager:
                self._manager.send_text(mesh_msg, channel_index=mesh_channel)

            self.log_activity(
                direction="radio_to_mesh",
                protocol=cfg.get("protocol", "dmr"),
                text=clean_text,
                radio_src_id=src_id,
                radio_dest_id=dst_id or filter_tg,
                mesh_channel=mesh_channel,
                duration_sec=round(duration, 2),
                status="completed"
            )
            logger.info(f"VoiceBridge STT -> Mesh: '{mesh_msg}'")

        except Exception as e:
            logger.error(f"VoiceBridge STT failed: {e}")
        finally:
            self._state = "idle"

    def transcribe_audio(self, pcm_bytes: bytes) -> str:
        """Run speech-to-text using faster-whisper or fallback."""
        cfg = self.get_config()
        model_name = cfg.get("whisper_model", "tiny.en")

        try:
            from faster_whisper import WhisperModel
            if self._whisper_model is None or self._whisper_model_name != model_name:
                logger.info(f"Loading faster-whisper model: {model_name}")
                self._whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
                self._whisper_model_name = model_name

            # Convert 8kHz PCM to in-memory WAV for Whisper
            wav_io = io.BytesIO()
            with wave.open(wav_io, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(pcm_bytes)
            wav_io.seek(0)

            segments, _ = self._whisper_model.transcribe(wav_io, beam_size=1, language="en")
            transcription = " ".join([seg.text.strip() for seg in segments])
            return transcription

        except ImportError:
            # Graceful simulation fallback when faster-whisper is not yet installed in host env
            logger.warning("faster-whisper is not installed. Using simulation mode.")
            return "[Voice Audio Received]"
        except Exception as e:
            logger.error(f"Error in Whisper transcription: {e}")
            return ""

    # --------------------------------------------------------------------------
    # TTS: Meshtastic Text -> Radio Voice
    # --------------------------------------------------------------------------
    def handle_meshtastic_inbound_text(self, sender_id: str, sender_name: str, text: str, channel: int) -> bool:
        """
        Called when a Meshtastic text arrives. If voice bridge & TTS are enabled
        for this channel, convert text to speech and transmit UDP audio to dvmbridge.
        """
        cfg = self.get_config()
        if cfg.get("enabled", "0") != "1" or cfg.get("tts_enabled", "1") != "1":
            return False

        target_channel = int(cfg.get("mesh_channel", "0"))
        if channel != target_channel:
            return False

        # Ignore bot/command messages
        if text.startswith("!") or text.startswith("🎙️"):
            return False

        threading.Thread(
            target=self._synthesize_and_transmit_tts,
            args=(sender_id, sender_name, text, channel),
            daemon=True
        ).start()
        return True

    def _synthesize_and_transmit_tts(self, sender_id: str, sender_name: str, text: str, channel: int):
        """Synthesize text using Piper TTS and stream 8kHz PCM to dvmbridge."""
        try:
            self._state = "transmitting"
            self._last_tts_time = time.time()
            cfg = self.get_config()
            target_tg = int(cfg.get("voice_talkgroup", "99"))
            tx_port = int(cfg.get("udp_tx_port", "32001"))
            tx_host = cfg.get("udp_host", "127.0.0.1")

            spoken_text = f"{sender_name} says: {text}"
            pcm_data = self.synthesize_speech(spoken_text)

            if not pcm_data:
                return

            # Transmit UDP PCM frames in 20ms chunks (160 samples = 320 bytes @ 8kHz 16-bit mono)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            chunk_size = 320
            duration = len(pcm_data) / (8000 * 2)

            for i in range(0, len(pcm_data), chunk_size):
                chunk = pcm_data[i:i + chunk_size]
                if len(chunk) < chunk_size:
                    chunk = chunk + b"\x00" * (chunk_size - len(chunk))

                # Prefix dvmbridge metadata header: [4B 'DVMB'][4B srcId=9000000][4B dstId=target_tg]
                header = struct.pack("!4sII", b"DVMB", 9000000, target_tg)
                sock.sendto(header + chunk, (tx_host, tx_port))
                time.sleep(0.019)  # 20ms pacing

            sock.close()
            self._last_tts_time = time.time()

            self.log_activity(
                direction="mesh_to_radio",
                protocol=cfg.get("protocol", "dmr"),
                text=text,
                radio_src_id=9000000,
                radio_dest_id=target_tg,
                mesh_node_id=sender_id,
                mesh_channel=channel,
                duration_sec=round(duration, 2),
                status="completed"
            )
            logger.info(f"VoiceBridge TTS -> Radio TG {target_tg}: '{spoken_text}' ({duration:.1f}s)")

        except Exception as e:
            logger.error(f"VoiceBridge TTS playback failed: {e}")
        finally:
            self._state = "idle"

    def synthesize_speech(self, text: str) -> bytes:
        """Synthesize text to 8kHz 16-bit mono PCM using Piper or fallback tone."""
        try:
            # Check for piper python library or binary
            import subprocess
            res = subprocess.run(
                ["piper", "--output_raw", "--model", "en_US-lessac-medium"],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=5
            )
            if res.returncode == 0 and res.stdout:
                return res.stdout
        except Exception:
            pass

        # Fallback generator: creates a clean audio notification burst (8kHz 16-bit PCM)
        import math
        sample_rate = 8000
        duration = 1.0
        pcm = bytearray()
        freq = 880.0  # A5 tone
        for i in range(int(sample_rate * duration)):
            sample = int(12000 * math.sin(2 * math.pi * freq * i / sample_rate))
            pcm.extend(struct.pack("<h", sample))
        return bytes(pcm)


# Singleton instance
voice_gateway = VoiceBridgeGateway()
