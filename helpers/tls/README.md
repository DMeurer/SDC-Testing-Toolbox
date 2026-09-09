# TLS Helper

`generate_certificates.py` creates everything needed to start one or more SDC
Testing Toolbox instances with mutually authenticated encrypted communication:

- `ca.pem`: a self-signed local certificate authority (CA)
- `ca-key.pem`: the CA signing key, required only to add more identities; keep it private
- `alpha.pem` and `alpha-key.pem`: one toolbox identity
- `beta.pem` and `beta-key.pem`: another toolbox identity
- `README.txt`: exact commands tailored to the generated filenames and IP address

Every toolbox process is both an SDC provider and consumer. There is no
provider-only or consumer-only certificate: every generated identity has both
TLS server and client authentication EKUs, so `alpha.pem`, `beta.pem`,
`gamma.pem`, and so on are interchangeable participant identities.

The participant keys are encrypted by default. Run from the repository root:

```powershell
.venv\Scripts\python.exe helpers\tls\generate_certificates.py
```

For a LAN address, include the actual address so it becomes a certificate IP SAN:

```powershell
.venv\Scripts\python.exe helpers\tls\generate_certificates.py --ip 192.168.1.42
```

Create more than the default `alpha` and `beta` identities with:

```powershell
.venv\Scripts\python.exe helpers\tls\generate_certificates.py --participants alpha beta gamma delta
```

To add `gamma` later without changing the CA, `alpha`, or `beta`, use the same
key-password choice as when the CA was created:

```powershell
.venv\Scripts\python.exe helpers\tls\generate_certificates.py --add gamma --ip 192.168.1.42
```

`--add` requires both `ca.pem` and the private `ca-key.pem`. A CA certificate
alone can validate identities but cannot create a new matching certificate. The
helper will not replace an existing participant identity in add mode.

The helper writes keys under `helpers/tls/generated/`, which Git ignores. It
will not overwrite an existing output directory unless `--force` is supplied,
and refuses to remove files it did not generate.

The generated `README.txt` contains one startup command per participant with
absolute, quoted PEM paths. For encrypted keys, enter the password in the GUI
or set `SDC_TOOLBOX_TLS_KEY_PASSWORD` for an unattended CLI launch. Do not pass
passwords as command-line arguments.

The generated CA and identities are intended for local testing only. They have
no revocation or rotation process and must not be reused as a production PKI.
