# Restricted Purchase Executor — install & operate (Hermes issue #65)

Everything here is **inert until Cal explicitly installs, stages credentials, and
launches**. No script enables a production flag, decrypts real credentials, or
makes a purchase.

## Files
| File | Purpose | Runs as |
|---|---|---|
| `hermes-purchase-executor.service` | Production one-shot unit. Static card creds encrypted; **ticket delivered dynamically** via a root-only tmpfs credential (never persistent). | root (systemd) |
| `hermes-purchase-executor-staging.service` | Synthetic, `--fake-e2e`, loopback-only staging run under the same hardening. | root (systemd) |
| `config.yaml` | Dedicated non-secret browser config (`sandbox_bypass: never`, cloud off, recording off). | — |
| `executor.env.example` | Non-secret env template (only `COGITATOR_BRIDGE_URL`). | — |
| `install.sh` | Idempotent: creates the unprivileged user, dirs, ACLs, installs both units **disabled**. No creds. | sudo |
| `stage-synthetic-credentials.sh` | Stages synthetic (public 4242) `staging-card_*` creds for the staging unit only. | sudo |
| `doctor.sh` | Read-only health: units present + disabled + inactive, config correct, env non-secret, credstore not readable by `v0id`, `systemd-analyze security`. | any (read-only) |
| `launch.sh` | Root one-shot launch helper: ticket from **stdin** → root-only tmpfs credential → one run → shred. | sudo |
| `uninstall.sh` | Idempotent rollback. Real card creds left in place unless `--purge-creds`. | sudo |

## Install (Cal, sudo)
```
sudo packaging/purchase-executor/install.sh
sudoedit /etc/hermes-purchase-executor/executor.env        # set COGITATOR_BRIDGE_URL
sudo packaging/purchase-executor/stage-synthetic-credentials.sh
packaging/purchase-executor/doctor.sh
```

## Synthetic staging run (proves the executor under real systemd hardening)
```
sudo systemctl start hermes-purchase-executor-staging.service
journalctl -u hermes-purchase-executor-staging -n 80    # expect fake_e2e: PASS
```
Synthetic creds only, temp Cogitator DB, in-process loopback bridge + mock
merchant, real local Chromium, loopback-only networking. No real merchant.

## Real purchase (separate, supervised, NOT covered by any script here)
1. Stage **real** card creds: `sudo systemd-creds encrypt --name=card_number - /etc/credstore.encrypted/card_number.cred` (and `card_expiry`, `card_cvv`, `card_name`, `cogitator_bridge_token`).
2. Enable `ENABLE_PURCHASE_OPERATOR_BRIDGE` and `ENABLE_PURCHASE_EXECUTOR_BRIDGE` on Cogitator.
3. Operator CLI (`scripts/purchase_operator_cli.py`): `propose → preview → approve → issue --launch`.
   The `issue --launch` pipes the ticket straight into `launch.sh`; the ticket is never printed or logged.
4. Executor stops on CAPTCHA/MFA/3DS; reconcile uncertain outcomes separately.
5. Disable both flags afterward.

## Rollback
```
sudo packaging/purchase-executor/uninstall.sh            # keeps real card creds
sudo packaging/purchase-executor/uninstall.sh --purge-creds   # also removes them
```
