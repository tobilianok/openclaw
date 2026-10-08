# MEMORY.md - Faits et décisions durables

Un résumé vivant, pas un journal.

## Infrastructure

- Hyperviseur : HPE ML150 Gen9, Proxmox VE, 192.168.1.10. Toutes les VM.
- NAS : srv-nas (EliteDesk 800 G3), 192.168.1.11, pool ZFS « archive », Jellyfin.
- Supervision : Prometheus, Alertmanager et Grafana sur 192.168.1.19.
  Les alertes arrivent dans Talk par un autre bot.
- Authentification : Authentik (auth.louisrousseaux.fr), TOTP obligatoire.
- L'Unraid est déclassé depuis le 27/09/2026.

## Décisions

- 2026-10-08 : le majordome tourne sur OpenClaw (VM dédiée), avec n8n pour
  les outils et les tâches planifiées. Il utilise uniquement des IA cloud
  gratuites, via un anonymiseur local qui pseudonymise tout ce qui sort.
- 2026-10-08 : rien ne tourne sur le PC de Louis (PC de jeu, usage perso).
