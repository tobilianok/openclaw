# MEMORY.md - Faits et decisions durables

Resume vivant, pas un journal. Rien de sensible (voir AGENTS.md).

## Infrastructure

- Hyperviseur : HPE ML150 Gen9, Proxmox VE, 192.168.1.10. Toutes les VM.
- NAS : srv-nas (EliteDesk 800 G3), 192.168.1.11, pool ZFS "archive", Jellyfin.
- Supervision : Prometheus / Alertmanager / Grafana sur 192.168.1.19.
  Les alertes arrivent dans Talk par un autre bot.
- Auth : Authentik (auth.louisrousseaux.fr), TOTP obligatoire.
- L'Unraid est declasse depuis le 27/09/2026.

## Decisions

- 2026-10-08 : le majordome tourne sur OpenClaw (VM dediee), avec n8n pour
  les outils et les taches planifiees. Modele local si le PC est allume,
  sinon IA cloud gratuite. Sujets sensibles : salon prive, 100 % local.
- 2026-10-08 : jamais de reveil a distance du PC de Louis.
