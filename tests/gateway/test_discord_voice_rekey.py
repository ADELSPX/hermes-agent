"""VoiceReceiver credential re-key behavior (issue #77968).

Discord voice is E2EE'd via DAVE (MLS): the group re-keys on any voice-channel
membership change, and a voice reconnect (server failover, region move) rotates
the transport ``secret_key``. ``VoiceReceiver.start()`` used to snapshot those
credentials once; after a re-key every decrypt failed silently forever and the
bot went deaf. These tests pin the contract: a decrypt-failure streak causes the
receiver to re-resolve credentials from the LIVE voice connection (never the
start() snapshot), failures stay visible in the logs, and with DAVE active an
unmapped SSRC never feeds ciphertext to the opus decoder.

Pure-python fakes throughout: a fake ``nacl.secret`` module with a real-enough
AEAD (the adapter only calls ``Aead(key).decrypt(ciphertext, header, nonce)``),
a fake ``davey`` namespace with epoch-tagged frames, and a fake
``discord.opus.Decoder``. The tests inject them via ``sys.modules`` before
driving ``VoiceReceiver._on_packet``.
"""

import struct
import sys
import types

import pytest

from plugins.platforms.discord.adapter import VoiceReceiver

BOT_SSRC = 9999
SSRC_A = 1111


class FakeAead:
    """Stand-in for nacl.secret.Aead. "Encryption" prefixes the frame with the
    key that encrypted it; decrypt fails unless the same key instance is used —
    exactly the property a stale snapshot violates after a re-key."""

    _boxes = []  # every key material an Aead was constructed with

    def __init__(self, key):
        if not isinstance(key, (bytes, bytearray)):
            raise TypeError("FakeAead requires bytes key")
        if len(key) != 32:
            raise ValueError("FakeAead requires a 32-byte key")
        self.key = bytes(key)
        FakeAead._boxes.append(self.key)

    def decrypt(self, encrypted, header, nonce):
        if len(encrypted) < 32:
            raise ValueError("ciphertext too short")
        if encrypted[:32] != self.key:
            raise ValueError("Decryption failed. Ciphertext failed verification")
        return bytes(encrypted[32:])


def _fake_nacl_module():
    mod = types.ModuleType("nacl")
    secret = types.ModuleType("nacl.secret")
    secret.Aead = FakeAead
    mod.secret = secret
    return mod


class FakeDaveSession:
    """Stand-in for a DAVE MLS session bound to one epoch: frames carry a
    one-byte epoch tag; a session only decrypts frames from its own epoch and
    raises on anything else (what a stale session does after a group re-key)."""

    def __init__(self, epoch=0, passthrough=False):
        self.epoch = epoch
        self.passthrough = passthrough
        self.decrypted_with_epochs = []

    def decrypt(self, user_id, media_type, data):
        if self.passthrough:
            raise RuntimeError("Unencrypted frame received")
        if bytes(data[:1]) != bytes([self.epoch]):
            raise RuntimeError("frame from another MLS epoch")
        self.decrypted_with_epochs.append(self.epoch)
        return bytes(data[1:])


def _fake_davey_module():
    mod = types.ModuleType("davey")

    class _MediaType:
        audio = "audio"

    mod.MediaType = _MediaType
    return mod


class FakeOpusDecoder:
    instances = []

    def __init__(self):
        self.decoded = []
        FakeOpusDecoder.instances.append(self)

    def decode(self, data):
        self.decoded.append(bytes(data))
        return b"\x00" * 3840  # 20ms of stereo 48k s16


@pytest.fixture
def voice_fakes(monkeypatch):
    monkeypatch.setitem(sys.modules, "nacl", _fake_nacl_module())
    monkeypatch.setitem(sys.modules, "nacl.secret", sys.modules["nacl"].secret)
    monkeypatch.setitem(sys.modules, "davey", _fake_davey_module())
    FakeAead._boxes = []
    FakeOpusDecoder.instances = []
    import plugins.platforms.discord.adapter as adapter_mod

    monkeypatch.setattr(
        adapter_mod.discord, "opus", types.SimpleNamespace(Decoder=FakeOpusDecoder)
    )


def make_key(n: int) -> bytes:
    return bytes([n]) * 32


class FakeVoiceConnection:
    """Mimics discord's voice connection: credentials the server can rotate."""

    def __init__(self, secret_key, dave_session, ssrc=BOT_SSRC):
        self.secret_key = list(secret_key)
        self.dave_session = dave_session
        self.ssrc = ssrc
        self.socket_listeners = []
        self.hook = None

    def add_socket_listener(self, fn):
        self.socket_listeners.append(fn)

    def remove_socket_listener(self, fn):
        if fn in self.socket_listeners:
            self.socket_listeners.remove(fn)

    def rekey(self, secret_key, dave_session=None):
        """Server-side re-key / DAVE epoch bump."""
        self.secret_key = list(secret_key)
        if dave_session is not None:
            self.dave_session = dave_session


def make_receiver(conn, allowed_user_ids=None):
    vc = types.SimpleNamespace(_connection=conn)
    return VoiceReceiver(vc, allowed_user_ids=allowed_user_ids)


def make_rtp(ssrc=SSRC_A, payload=b"hello", key=make_key(1), seq=0):
    """Build an RTP voice packet the way the adapter expects:
    header || encrypted (key-tag || payload) || 4 nonce bytes."""
    header = struct.pack(">BBHII", 0x80, 0x78, seq, 12345, ssrc)  # v2, PT 0x78, no ext
    return header + key + payload + b"\x01\x02\x03\x04"


def all_decoded_audio():
    return b"".join(
        d for inst in FakeOpusDecoder.instances for d in inst.decoded
    )


# ---------------------------------------------------------------------------
# Credential re-resolution from the live connection
# ---------------------------------------------------------------------------


class TestCredentialRefreshOnRekey:
    def test_transport_rekey_recovered_from_live_connection(self, voice_fakes):
        """After a transport re-key, packets under the NEW key must reach the opus
        decoder: a decrypt-failure streak makes the receiver re-resolve
        secret_key from the live connection instead of failing forever on the
        start() snapshot."""
        key1, key2 = make_key(1), make_key(2)
        conn = FakeVoiceConnection(key1, None)
        receiver = make_receiver(conn)
        receiver.start()

        conn.rekey(key2)  # server rotates the transport key post-start
        for seq in range(30):
            receiver._on_packet(make_rtp(key=key2, seq=seq))

        assert FakeAead._boxes, "no decrypt was ever attempted"
        assert FakeAead._boxes[-1] == key2, (
            "receiver never re-resolved secret_key from the live connection"
        )
        receiver._on_packet(make_rtp(key=key2, payload=b"recovered", seq=99))
        assert b"recovered" in all_decoded_audio(), (
            "packet encrypted under the post-rekey key was never decoded"
        )

    def test_dave_session_replacement_is_picked_up(self, voice_fakes):
        """After a reconnect the connection carries a NEW DAVE session (new MLS
        epoch); a DAVE decrypt-failure streak must make the receiver adopt the
        live session, not the dead captured one."""
        key = make_key(1)
        old_session = FakeDaveSession(epoch=0)
        conn = FakeVoiceConnection(key, old_session)
        receiver = make_receiver(conn)
        receiver.start()
        receiver.map_ssrc(SSRC_A, 42)

        new_session = FakeDaveSession(epoch=1)
        conn.rekey(key, dave_session=new_session)
        # Frames from epoch 1 fail against the captured epoch-0 session...
        for seq in range(30):
            receiver._on_packet(
                make_rtp(key=key, seq=seq, payload=b"\x01audio")
            )
        # ...but the streak must refresh, and the live session must decrypt.
        receiver._on_packet(
            make_rtp(key=key, payload=b"\x01speech", seq=99)
        )
        assert new_session.decrypted_with_epochs, (
            "DAVE decrypt never ran on the live (post-reconnect) session"
        )
        assert all(e == 1 for e in new_session.decrypted_with_epochs)
        assert b"speech" in all_decoded_audio()

    def test_refresh_survives_connection_swap(self, voice_fakes):
        """A voice reconnect replaces the whole connection object; the refresh
        path must read the CURRENT connection, so decoding continues against it."""
        key = make_key(7)
        conn_old = FakeVoiceConnection(make_key(1), None)
        vc = types.SimpleNamespace(_connection=conn_old)
        receiver = VoiceReceiver(vc)
        receiver.start()

        conn_new = FakeVoiceConnection(key, None)
        vc._connection = conn_new  # reconnect swapped the connection object

        for seq in range(30):
            receiver._on_packet(make_rtp(key=key, seq=seq))
        receiver._on_packet(make_rtp(key=key, payload=b"after-swap", seq=99))
        assert b"after-swap" in all_decoded_audio(), (
            "receiver never adopted the replacement connection's credentials"
        )

    def test_refresh_preserves_working_credentials(self, voice_fakes):
        """Refreshing is safe: while credentials still match, packets keep decoding."""
        key = make_key(1)
        conn = FakeVoiceConnection(key, None)
        receiver = make_receiver(conn)
        receiver.start()
        receiver._on_packet(make_rtp(key=key, payload=b"steady"))
        assert b"steady" in all_decoded_audio()


# ---------------------------------------------------------------------------
# DAVE-active unmapped SSRC: never feed ciphertext to opus
# ---------------------------------------------------------------------------


class TestDaveActiveUnmappedSsrc:
    def test_ciphertext_never_reaches_opus_when_dave_active(self, voice_fakes):
        """With DAVE active and an SSRC not yet mapped (no SPEAKING event), the
        NaCl-decrypted frame is still DAVE ciphertext — it must NOT reach the
        opus decoder (pre-fix it shredded that speaker's audio)."""
        key = make_key(1)
        session = FakeDaveSession(epoch=0)
        conn = FakeVoiceConnection(key, session)
        receiver = make_receiver(conn)
        receiver.start()
        receiver._on_packet(make_rtp(key=key, payload=b"\x00opaque"))
        assert not FakeOpusDecoder.instances, (
            "DAVE ciphertext for an unmapped SSRC reached the opus decoder"
        )

    def test_mapped_ssrc_still_decodes_when_dave_active(self, voice_fakes):
        """The guard must not over-block: once SPEAKING maps the SSRC, normal
        DAVE-decrypted audio flows to opus."""
        key = make_key(1)
        session = FakeDaveSession(epoch=0)
        conn = FakeVoiceConnection(key, session)
        receiver = make_receiver(conn)
        receiver.start()
        receiver.map_ssrc(SSRC_A, 42)
        receiver._on_packet(make_rtp(key=key, payload=b"\x00speech"))
        assert b"speech" in all_decoded_audio()

    def test_unencrypted_passthrough_still_decodes(self, voice_fakes):
        """When DAVE reports the frame as Unencrypted passthrough, audio flows
        to opus — that path must survive the guard."""
        key = make_key(1)
        session = FakeDaveSession(epoch=0, passthrough=True)
        conn = FakeVoiceConnection(key, session)
        receiver = make_receiver(conn)
        receiver.start()
        receiver.map_ssrc(SSRC_A, 42)
        receiver._on_packet(make_rtp(key=key, payload=b"raw"))
        assert b"raw" in all_decoded_audio()

    def test_no_dave_session_decodes_directly(self, voice_fakes):
        """Without a DAVE session (DAVE not active), unmapped SSRCs decode via
        NaCl → opus as before — the guard applies only when DAVE is active."""
        key = make_key(1)
        conn = FakeVoiceConnection(key, None)
        receiver = make_receiver(conn)
        receiver.start()
        receiver._on_packet(make_rtp(key=key, payload=b"plain"))
        assert b"plain" in all_decoded_audio()


# ---------------------------------------------------------------------------
# Visibility: failures no longer go fully dark
# ---------------------------------------------------------------------------


class TestDecryptFailureVisibility:
    def test_failure_streak_is_logged_beyond_first_ten(self, voice_fakes):
        """Pre-fix behavior: warnings stopped after the first 10 packets, so a
        deaf session was undiagnosable. Post-fix, a persistent failure streak
        keeps emitting warnings on a fixed cadence."""
        from unittest.mock import MagicMock

        import plugins.platforms.discord.adapter as adapter_mod

        key1, key2 = make_key(1), make_key(2)
        conn = FakeVoiceConnection(key1, None)
        receiver = make_receiver(conn)
        receiver.start()
        # Simulate prior traffic so the legacy first-10-packets window is spent.
        receiver._packet_debug_count = 10

        warns = []
        original = adapter_mod.logger.warning
        adapter_mod.logger.warning = MagicMock(
            side_effect=lambda *a, **k: warns.append(a)
        )
        try:
            conn.rekey(key2)
            for seq in range(50):
                receiver._on_packet(make_rtp(key=key2, seq=seq))
        finally:
            adapter_mod.logger.warning = original

        assert any(
            "decrypt" in " ".join(str(a) for a in args).lower() for args in warns
        ), "no decrypt-failure warning logged for a stale-credential streak"


# ---------------------------------------------------------------------------
# start()/stop() contract with the live connection
# ---------------------------------------------------------------------------


class TestStartStop:
    def test_start_resolves_live_credentials(self, voice_fakes):
        key = make_key(1)
        conn = FakeVoiceConnection(key, None)
        receiver = make_receiver(conn)
        receiver.start()
        assert receiver._secret_key == key
        assert receiver._bot_ssrc == BOT_SSRC
        assert receiver._on_packet in conn.socket_listeners

    def test_stop_removes_listener(self, voice_fakes):
        key = make_key(1)
        conn = FakeVoiceConnection(key, None)
        receiver = make_receiver(conn)
        receiver.start()
        receiver.stop()
        assert receiver._on_packet not in conn.socket_listeners
