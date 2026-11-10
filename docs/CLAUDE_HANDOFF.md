# Relais Claude — Autonomous AI Company

Dépôt : https://github.com/Julien-RDS/autonomous-ai-company

Lire CLAUDE.md, README.md, [AVANCEMENT.md](AVANCEMENT.md),
[AUDIT_GLOBAL_2026_11.md](AUDIT_GLOBAL_2026_11.md), puis
[PROMPT_CLAUDE_FINALISATION.md](PROMPT_CLAUDE_FINALISATION.md).

Les lots persistance, reprise, sandbox, supervision et actions web locales sont implémentés. L’ancien
ordre « commencer par le Lot 1 » est remplacé par la finalisation du pilotage web
sécurisé. Vérifier références Git et CI ; ne pas réappliquer les anciens bundles.

Les corrections d’audit sont sur feat/project-readiness-audit. Si elles ne sont
pas fusionnées, créer la branche de travail depuis cette branche et indiquer cette
dépendance dans la PR. Conserver MOCK_MODE=true, aucun appel payant en test.
Ne jamais présenter un test ignoré ou une fonctionnalité prévue comme validé.
