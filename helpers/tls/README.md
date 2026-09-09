# TLS Helper

`generate_certificates.py` creates everything needed to start two SDC Testing
Toolbox instances with mutually authenticated encrypted communication:

- `ca.pem`: a self-signed local certificate authority (CA)
- `provider.pem` and `provider-key.pem`: provider identity
- `consumer.pem` and `consumer-key.pem`: consumer identity
- `README.txt`: exact commands tailored to the generated filenames and IP address

The participant keys are encrypted by default. Run from the repository root:

```powershell
.venv\Scripts\python.exe helpers\tls\generate_certificates.py
```

For a LAN address, include the actual address so it becomes a certificate IP SAN:

```powershell
.venv\Scripts\python.exe helpers\tls\generate_certificates.py --ip 192.168.1.42
```

The helper writes keys under `helpers/tls/generated/`, which Git ignores. It
will not overwrite an existing output directory unless `--force` is supplied,
and refuses to remove files it did not generate.

Start both toolbox instances from that output directory so the relative PEM
paths in its generated `README.txt` resolve correctly. For encrypted keys,
enter the password in the GUI or set `SDC_TOOLBOX_TLS_KEY_PASSWORD` for an
unattended CLI launch. Do not pass passwords as command-line arguments.

The generated CA and identities are intended for local testing only. They have
no revocation or rotation process and must not be reused as a production PKI.
