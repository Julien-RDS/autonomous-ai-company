# État actuel — 10 novembre 2026

Le projet est un **MVP de laboratoire supervisé** : débats, décisions, prototypes
approuvés et preuves techniques. Il ne mesure pas de revenus réels et ne dirige
pas une activité commerciale.

PR #7 (supervision) puis PR #8 (actions web locales) intégrées dans main,
à ea7dc9d. CI GitHub verte :
https://github.com/Julien-RDS/autonomous-ai-company/actions/runs/42789168581
Les corrections d’audit sont sur `feat/project-readiness-audit` ; cela ne signifie
pas leur fusion dans main.

| Composant | Utilisable aujourd’hui | Limite actuelle |
|---|---|---|
| Conseil OpenAI / Anthropic / Gemini | Démonstration sans clé ; connecteurs API implémentés | Appels payants non validés pendant cet audit |
| Proposition, recherche, critique, vote, décision | Cycle borné, rapports et tickets | Le débat ne prouve ni demande ni qualité |
| Persistance et migrations | SQLite local ; PostgreSQL 16 pour reprise durable | Sauvegardes à organiser sur la machine cible |
| File, leases, checkpoints, reprise après crash | Réunions et génération MOCK | Worker réel et sandbox en file absents |
| Budget EUR | Decimal, réservations, incertitudes et rapprochement humain | Estimation locale ; plafonds fournisseur séparés |
| Prototypes | Approbation, manifeste et publication atomique | Aucun lancement automatique du code |
| Sandbox | Python unittest et validation statique HTML ; approbation dédiée à usage unique | Docker doit appliquer les limites réelles ; HTML ne teste pas JavaScript |
| Supervision web | Réunions, tâches, file, budget, rapports, sandbox | Consultation locale sans authentification |
| Actions web locales optionnelles | Jeton administrateur, session signée, CSRF, permissions distinctes | Traitements longs dans HTTP, pas de registre durable des demandes ni de révocation individuelle des sessions |
| Planification | Scheduler quotidien tant que son processus tourne | Une instance ; pas de verrou global |
| Évaluation | Résultats humains et routage expérimental | Pas de benchmark indépendant ni de validation commerciale automatisée |
| GitHub, navigateur agentique, emails, paiements, publication | Absents de l’application | Chantiers distincts, hors MVP supervisé |

## Éléments vérifiés

Voir [l’audit global](AUDIT_GLOBAL_2026_11.md) pour les défauts reproduits, les tests,
le parcours Chromium et les limites cloud. Les anciennes notes de lots décrivent
leur état à la livraison : leurs compteurs et mentions « non fusionné » ne sont
pas l’état actuel.

## Dernier chantier du MVP pilotable

La [mission Claude](PROMPT_CLAUDE_FINALISATION.md) porte sur la **durabilité des actions web et
la reprise après crash** à partir de la console authentifiée existante. Elle doit empêcher les doublons et traiter
les crashs sans rejouer une exécution ambiguë. Les workers payants restent désactivés.

Après validation, l’objectif est un outil personnel pilotable depuis le navigateur
sur une machine maîtrisée. Une entreprise autonome rentable, un service public
multi-clients et une exploitation payante 24 h/24 restent hors de cette définition
de fini.
