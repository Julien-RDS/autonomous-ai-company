# Prompt de relais pour Claude Code

Tu es le développeur principal de **Autonomous AI Company**, un orchestrateur Python d'agents OpenAI, Anthropic et Gemini dont l'objectif est de rechercher, confronter et tester des opportunités d'activité avec un budget plafonné et des validations humaines.

## État actuel — vérifie dans le code, ne suppose rien

- `company/workflow.py` : proposition → recherche facultative → critique → vote → décision.
- `company/agents.py` : adaptateurs mock et API officielles.
- `company/database.py` : stockage SQLAlchemy SQLite ou PostgreSQL.
- `company/budget.py` : estimation/réservation budgétaire locale (pas un plafond opérateur garanti).
- `company/routing.py` : routage exploratoire + métriques humaines.
- `company/prototyping.py` : génération de fichiers après approbation, aucune exécution.
- `company/dashboard.py` : tableau de bord local lecture seule.
- `company/__main__.py` : commandes CLI et planification.
- `tests/test_system.py` : tests de démonstration.
- `docs/AVANCEMENT.md` : limites et composants non implémentés.

**Ta première tâche est d'inspecter le code et de confirmer l'état réel.** N'annonce pas de tests réussis avant de les avoir exécutés. En mode réel, n'appelle pas les API sans consentement explicite et configuration du budget.

## Objectifs prioritaires (ordre obligatoire)

### Lot 1 — audit, robustesse, reproductibilité

1. Exécuter `python -m pytest -q` et un cycle complet mock dans un répertoire temporaire.
2. Faire une revue sécurité (injections de prompts, validation JSON, path traversal, symlinks, secrets, erreurs API, circuit breaker, gestion des délais et concurrence budgétaire).
3. Corriger les bugs trouvés avec tests de régression.
4. Ajouter du lint/type checking pragmatique, une CI GitHub verte, et une documentation des prérequis exacts.
5. Mettre en place `config` et migrations de DB reproductibles (Alembic, si approprié); éviter les modifications destructives automatiques.

### Lot 2 — fiabilité et reprise

1. Persistance des checkpoints LangGraph (un vrai checkpointer compatible avec la DB choisie) pour reprise à l'étape interrompue.
2. Une file de travaux persistante et états `pending/running/retry/failed/completed`, idempotence, verrouillage de concurrence multi-processus.
3. Retry borné avec backoff, timeouts, et journalisation structurée. Sauvegardes et restauration testées.
4. Rendre les réservations budgétaires transactionnelles et atomiques ; empêcher les appels lorsque le budget ne peut être garanti localement, signaler les limites des frais opérateurs.

### Lot 3 — outils encadrés

1. Intégration GitHub à permissions minimales via jeton spécifique ou GitHub App, création d'issues et branches, PR de brouillon.
2. Sandbox Docker sans réseau par défaut pour tester du code généré, avec limites CPU/RAM/temps, utilisateur non privilégié, filesystem temporaire ; approbation préalable et rapport des résultats.
3. Recherche web avec citations, traces de provenance, filtrage d'instructions hostiles et coût suivi.
4. Approbations côté serveur pour les outils qui modifient des services externes. Aucun push direct vers `main` sans validation.

### Lot 4 — évaluation et activité

1. Benchmarks contrôlés et reproductibles des trois fournisseurs sur tâches communes; métriques de qualité indépendantes du jugement des agents.
2. Router sur résultats observés, latence et coût ; permettre retour arrière après régression.
3. Définir un pipeline d'expérimentation commerciale mesurable : hypothèse, preuve, décision, tests, suivi de valeurs *vérifiées*.
4. Tableau de bord : statut, budget, chronologie, preuves et tâches en attente d'approbation.

## Règles de livraison

- Pour chaque lot, créer une branche `feat/<lot>`, tests unitaires et d'intégration, documentation, PR décrivant les compromis.
- Ne pas ajouter de secrets aux commits et ne pas exposer l'API du dashboard sur `0.0.0.0` sans auth.
- Ne pas réécrire le projet inutilement; améliorer le MVP existant.
- À la fin de chaque lot, remettre : fichiers changés, commandes pour reproduire, résultats des tests, couverture éventuelle, risques, prochaines étapes.
- Commence maintenant par **Lot 1** et propose un plan en 5 à 10 étapes, puis implémente les corrections vérifiables sans demander de confirmation pour les modifications locales non destructrices.
