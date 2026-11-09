# État d'avancement par rapport au cahier des charges

## Phase 1 — Communication entre modèles : IMPLÉMENTÉE

- Trois connecteurs officiels : OpenAI Responses API, Anthropic Messages, Gemini GenerateContent.
- Débat borné à trois étapes (proposition / critique / vote).
- Synthèse déterministe par vote, égalités signalées.
- Simulation déterministe sans clé.

## Phase 2 — Mémoire et autonomie : PARTIELLEMENT IMPLÉMENTÉE

- SQLite ou PostgreSQL : réunions, messages, dépenses, tickets, feedbacks.
- Planification quotidienne avec APScheduler.
- Récupération des décisions des 3 réunions terminées précédentes.
- Lot 2 : reprise LangGraph PostgreSQL, file SQL durable, leases, heartbeats, tentatives bornées et audit implémentés pour les workflows MOCK. Sauvegardes documentées ; automatisation et activation des workers réels restent à réaliser.

## Phase 3 — Outils et exécution : PARTIELLEMENT IMPLÉMENTÉE

- Recherche Tavily facultative, bornée et comptabilisée avec un coût paramétré.
- Écriture de fichiers de prototypes locaux après approbation.
- Lot 3 : tests isolés des prototypes dans Docker, après approbation d'exécution dédiée et à usage unique (voir `LOT_3_SANDBOX.md`).
- Manquent : connexion GitHub, navigateur agentique, profils de test autres que Python, exécution depuis la file durable et déploiement.
- Aucune commande système issue d'une IA n'est exécutée sur l'hôte ; seule la commande de test fixée par l'application tourne dans le conteneur.

## Phase 4 — Intelligence économique : ÉBAUCHE

- Hypothèses de marché / tests à mener / critères de validation / recherche de concurrence.
- Manquent : données terrain réelles, CRM, vérification des revenus, prospection consentie avec contrôles, mesure commerciale intégrée.

## Phase 5 — Auto-optimisation : ÉBAUCHE

- Statistiques de vitesse/erreurs et enregistrement d'évaluations humaines.
- Routage exploratoire de prototypes puis adaptation après au moins 3 exemples par fournisseur.
- Manquent : benchmarks reproductibles communs, analyse de qualité indépendante, optimisation des workflows avec revue humaine.

## Sécurité

- Mode démo par défaut.
- Refus du mode réel tant que clés, modèles et tarifs ne sont pas renseignés.
- Contrôle budgétaire prévisionnel local ; les limites **réelles** doivent également être configurées chez les fournisseurs.
- Tâches nécessitant approbation humaine.
- Aucun paiement, email, contrat ou publication ; aucun code produit par un modèle exécuté hors du sandbox Docker approuvé pour ce lancement précis.

## Statut de vérification

- Tests unitaires/intégration de la démonstration : réussis dans l'environnement de génération.
- Tableau de bord et chaîne complète de production de fichiers : testés en mode simulation.
- Intégration réelle des SDK, LangGraph officiel, PostgreSQL et APScheduler : code écrit mais **non testé ici** (l'accès réseau de l'environnement de génération n'a pas permis d'installer les dépendances manquantes). Une validation sur Ubuntu/WSL est nécessaire après `pip install -r requirements.txt`.

## Validation du Lot 2

Migrations vierges/historiques, Decimal, réservations concurrentes, leases,
checkpoints et crashs de processus testés avec PostgreSQL 16 réel.
Démarrage Compose (migration puis worker) et workflow MOCK validés.
Voir `LOT_2_PERSISTENCE.md` pour le protocole et les limites ; aucune API payante
n'a été contactée. Le résultat de la CI distante n'est pas encore observé.

## Complément après revue des Lots 1/2

Les prototypes MOCK approuvés peuvent être soumis à la file durable : verrou
commun avec la CLI, manifeste persistant et reprise après publication interrompue.
Le rapprochement humain des coûts inconnus est disponible avec référence d'audit.
Voir `REVIEW_AND_DURABLE_PROTOTYPES.md` pour les limites et le blocage réseau GitHub.

## Lot 3 — tests isolés (branche feat/lot-3-sandbox)

Revue du socle : fuite de secrets via `repr(Settings)` et mise en dead-letter
d'un travail dont l'ancien propriétaire était encore vivant, corrigées avec tests.
Exécuteur Docker isolé, approbation d'exécution à usage unique, migration additive
006 et commandes `sandbox`. Validé localement sous Python 3.11/3.12 avec
PostgreSQL 16 et Docker réels ; CI distante de la branche à observer après push.
