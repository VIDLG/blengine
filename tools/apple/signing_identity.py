"""Read-only correlation of valid signing identities with public certificate OUs."""

from __future__ import annotations

import hashlib
import re
import ssl
import subprocess
import tempfile
from pathlib import Path


def _run(command: list[str]) -> str:
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(
            f"read-only signing query failed (exit {result.returncode}): "
            f"{Path(command[0]).name} {command[1]}: {result.stderr.strip()}"
        )
    return result.stdout


def visible_development_identities(
    security: str, openssl: str, *, keychain: str | None = None
) -> tuple[int, dict[str, str]]:
    """Return visible identity count and fingerprint→Team from certificate OU only.

    A public certificate alone is insufficient: its DER SHA-1 must occur in the
    current valid identity enumeration in the same keychain scope. This does not
    attempt signing, unlock a keychain or prove private-key access.
    """
    scope = [keychain] if keychain is not None else []
    identities = _run([security, "find-identity", "-v", "-p", "codesigning", *scope])
    count = re.search(r"^\s*(\d+) valid identities found\s*$", identities, re.MULTILINE)
    entries = re.findall(
        r'^\s*\d+\)\s+([A-Fa-f0-9]{40})\s+"([^"\n]+)"\s*$',
        identities,
        re.MULTILINE,
    )
    if count is None or int(count.group(1)) == 0 or not entries:
        raise RuntimeError(
            "no visible valid code-signing identity in the queried keychain scope; "
            "this does not establish absence of public certificates or key usability"
        )
    development = {
        fingerprint.upper()
        for fingerprint, name in entries
        if name.startswith("Apple Development:")
    }
    certificates = _run(
        [security, "find-certificate", "-a", "-c", "Apple Development", "-p", *scope]
    )
    teams: dict[str, str] = {}
    for pem in re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        certificates,
        re.DOTALL,
    ):
        der = ssl.PEM_cert_to_DER_cert(pem)
        fingerprint = hashlib.sha1(der, usedforsecurity=False).hexdigest().upper()
        if fingerprint not in development or fingerprint in teams:
            continue
        # Only public certificate material is written; never export a private key.
        with tempfile.TemporaryDirectory(prefix="blehub-signing-certificate-") as root:
            certificate = Path(root) / "public.pem"
            certificate.write_text(pem + "\n", encoding="ascii")
            subject = _run(
                [
                    openssl,
                    "x509",
                    "-noout",
                    "-subject",
                    "-nameopt",
                    "sep_multiline,oid,utf8",
                    "-in",
                    str(certificate),
                ]
            )
        ous = re.findall(
            r"^[ \t]*2\.5\.4\.11[ \t]*=[ \t]*(.*?)[ \t]*$",
            subject,
            re.MULTILINE,
        )
        if len(ous) != 1 or re.fullmatch(r"[A-Z0-9]{10}", ous[0]) is None:
            raise RuntimeError(
                "matching valid identity's public certificate must have exactly one "
                "10-character Team OU (2.5.4.11); CN is not a Team fallback"
            )
        teams[fingerprint] = ous[0]
    return int(count.group(1)), teams
