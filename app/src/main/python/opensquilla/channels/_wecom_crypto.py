"""WeCom callback crypto — vendored AES-256-CBC + PKCS7 + msg-signature.

Implements the corp-app callback envelope per Tencent's public spec at
``https://developer.work.weixin.qq.com/document/path/90968`` (encryption
scheme) and ``https://developer.work.weixin.qq.com/document/path/90930``
(signature). OpenSquilla vendors this rather than depending on ``wechatpy`` so
we own the whole surface against the published spec, get native ``async``
HTTP, and avoid a 2021-frozen library.

Wire format (Tencent corp-app callbacks):

* ``EncodingAESKey`` is the 43-character base64 key from the corp-app
  console. Decode as ``base64.b64decode(key + "=")`` — appending one
  ``=`` recovers the 32-byte AES-256 key (43 chars + 1 pad = 44 chars,
  the canonical Tencent input form).
* Plaintext block = ``random(16) || msg_len(4 BE) || msg || receiver_id``
  PKCS7-padded to a 32-byte AES block size.
* Cipher = AES-256-CBC with ``iv = key[:16]``.
* Signature = ``sha1(sorted([token, timestamp, nonce, encrypt]).join(""))``
  hex digest, verified in constant time.

The public surface is intentionally narrow: ``WeComCrypto`` carries the
token + receiver_id (the corp_id), and exposes ``decrypt_message``,
``encrypt_message``, ``verify_signature``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_BLOCK_SIZE = 32  # AES block size for PKCS7 padding (Tencent uses 32-byte blocks)


def _pkcs7_pad(data: bytes, block_size: int = _BLOCK_SIZE) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    if pad_len == 0:
        pad_len = block_size
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data: bytes, block_size: int = _BLOCK_SIZE) -> bytes:
    if not data:
        raise ValueError("WeCom decrypt: empty plaintext")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > block_size:
        # Bad padding strongly indicates a wrong key, mismatched receiver_id,
        # or corruption upstream. Surface it explicitly so the webhook
        # handler emits ``wecom.signature_invalid`` instead of returning a
        # silently-malformed plaintext.
        raise ValueError(f"WeCom decrypt: invalid PKCS7 padding length {pad_len}")
    return data[:-pad_len]


def _decode_aes_key(encoding_aes_key: str) -> bytes:
    """Decode the 43-char Tencent EncodingAESKey to a 32-byte AES key."""
    # Tencent appends one "=" to make standard base64. Allow either form.
    padded = encoding_aes_key if encoding_aes_key.endswith("=") else encoding_aes_key + "="
    key = base64.b64decode(padded)
    if len(key) != 32:
        raise ValueError(f"WeCom EncodingAESKey must decode to 32 bytes, got {len(key)}")
    return key


@dataclass(frozen=True)
class WeComCrypto:
    """Vendored WeCom corp-app callback crypto.

    Constructor inputs match the corp-app console fields:

    * ``token`` — callback token (used for sha1 msg-signature).
    * ``encoding_aes_key`` — 43-char base64 AES key from the console.
    * ``receiver_id`` — typically the corp_id; tail of the plaintext
      envelope and validated on decrypt.
    """

    token: str
    encoding_aes_key: str
    receiver_id: str

    # ------------------------------------------------------------------
    # Signature
    # ------------------------------------------------------------------

    @staticmethod
    def compute_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
        parts = sorted([token, timestamp, nonce, encrypt])
        return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    @classmethod
    def verify_signature(
        cls,
        token: str,
        timestamp: str,
        nonce: str,
        encrypt: str,
        signature: str,
    ) -> bool:
        """Constant-time SHA1 signature check."""
        expected = cls.compute_signature(token, timestamp, nonce, encrypt)
        return hmac.compare_digest(expected, signature)

    # ------------------------------------------------------------------
    # Encrypt / decrypt
    # ------------------------------------------------------------------

    def _aes_key(self) -> bytes:
        return _decode_aes_key(self.encoding_aes_key)

    def decrypt_message(self, encrypt_b64: str) -> str:
        """Decrypt a base64-encoded ciphertext envelope and return the inner message.

        Validates the trailing receiver_id matches ``self.receiver_id``.
        """
        key = self._aes_key()
        iv = key[:16]
        ciphertext = base64.b64decode(encrypt_b64)
        decryptor = Cipher(algorithms.AES256(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        plaintext = _pkcs7_unpad(padded)

        if len(plaintext) < 20:
            raise ValueError("WeCom decrypt: plaintext too short")
        # plaintext = random(16) || msg_len(4 BE) || msg || receiver_id
        msg_len = struct.unpack(">I", plaintext[16:20])[0]
        msg = plaintext[20 : 20 + msg_len].decode("utf-8")
        receiver = plaintext[20 + msg_len :].decode("utf-8")
        if receiver != self.receiver_id:
            raise ValueError(
                "WeCom decrypt: receiver_id mismatch "
                f"(expected {self.receiver_id!r}, got {receiver!r})"
            )
        return msg

    def encrypt_message(
        self,
        reply_xml: str,
        nonce: str,
        timestamp: str | None = None,
    ) -> str:
        """Encrypt a reply XML body and wrap it in a signed Tencent envelope.

        Returns a fully-formed XML response ready to ship back to Tencent.
        """
        ts = timestamp or str(int(time.time()))
        key = self._aes_key()
        iv = key[:16]
        random_prefix = os.urandom(16)
        msg_bytes = reply_xml.encode("utf-8")
        msg_len = struct.pack(">I", len(msg_bytes))
        receiver_bytes = self.receiver_id.encode("utf-8")
        payload = random_prefix + msg_len + msg_bytes + receiver_bytes
        padded = _pkcs7_pad(payload)
        encryptor = Cipher(algorithms.AES256(key), modes.CBC(iv)).encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        encrypt_b64 = base64.b64encode(ciphertext).decode("ascii")
        signature = self.compute_signature(self.token, ts, nonce, encrypt_b64)
        return (
            "<xml>"
            f"<Encrypt><![CDATA[{encrypt_b64}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
            f"<TimeStamp>{ts}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            "</xml>"
        )


def generate_nonce(length: int = 16) -> str:
    """Hex nonce helper for replies."""
    return secrets.token_hex(length // 2)
