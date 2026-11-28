"""Autorité de certification jetable de la cible v1 (F-008, BR-010) : la passerelle exige https vers
un fournisseur OAuth (BR-084) ; l'amont du banc est servi en TLS par un certificat qu'elle émet pour
le nom `upstream`. Joué DANS l'image construite (sa bibliothèque `cryptography`), écrit dans le
dossier donné : `ca.pem` (monté comme magasin de certificats de la passerelle, dans
`v1.compose.yaml` seulement), `upstream.pem`, `upstream.key`. Détruits au teardown avec `.run/`."""
import datetime
import os
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def main(folder):
    os.makedirs(folder, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "autorite jetable du banc v1")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "upstream")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("upstream")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    with open(os.path.join(folder, "ca.pem"), "wb") as f:
        f.write(ca.public_bytes(pem))
    with open(os.path.join(folder, "upstream.pem"), "wb") as f:
        f.write(leaf.public_bytes(pem))
    with open(os.path.join(folder, "upstream.key"), "wb") as f:
        f.write(key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    for name in ("ca.pem", "upstream.pem", "upstream.key"):
        os.chmod(os.path.join(folder, name), 0o444)


if __name__ == "__main__":
    main(sys.argv[1])
