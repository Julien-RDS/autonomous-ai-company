# Lot 1 — audit et fiabilisation

## Diagnostic initial

Base : `origin/main` à `cea210d2dcc6cbe5dff9a501ee051e83d1c0d4e1`.
Lecture de README, CLAUDE, CLAUDE_HANDOFF, AVANCEMENT, configuration,
modules Python, tests et workflow CI. Aucun fichier prioritaire manquant.
Python 3.12.14, dépendances installées : `pip check` sans conflit ; 5 tests initiaux passants.

Le mode mock produit réellement neuf messages, une décision, un rapport et deux
tâches SQLite ; les propositions et votes sont simulés, la demande commerciale
n'est pas vérifiée. LangGraph officiel est installé ; le repli minigraph reste
séquentiel. Dashboard local en lecture seule ; génération de fichiers conditionnée
à une approbation. Aucune exécution automatique de code généré.

Les SDK réels existent mais ne sont pas validés contre les services payants.
PostgreSQL, recherche Tavily et planificateur ne sont pas validés en intégration.
Aucune dépendance requise manquante sur cette machine ; requirements contient des
plages de versions et non un verrou complet. La documentation AVANCEMENT décrit
les limites correctement ; sa rubrique historique de tests ne constitue pas une
preuve de validation dans cette machine.

## Corrections

- Erreurs fournisseurs encapsulées : aucun texte brut SDK dans messages, incidents,
  exceptions publiques ou journaux. Logs structurés par fournisseur, étape, réunion
  et classe d'erreur ; pas de prompt ou clé. Un seul essai par appel pour éviter
  une répétition facturée ; conserver la réservation si l'issue est inconnue.
- Gemini : délai de 45 secondes par requête et un seul essai configurés explicitement,
  comme OpenAI et Anthropic. Vérification des adaptateurs par clients factices.
- Budget : refuser réservations nulles/négatives/non finies, coûts négatifs/non finis
  et double régularisation séquentielle. Contrôle et insertion dans la même
  transaction : BEGIN IMMEDIATE SQLite, verrou consultatif transactionnel PostgreSQL.
- Rapport écrit avant validation transactionnelle de la réunion et des tâches :
  un disque indisponible ne laisse plus des tâches liées à une réunion échouée.
- CI : ajout du contrôle des dépendances et de compilation Python ; matrice
  Python 3.11/3.12 conservée, simulation sans appels payants.

## Vérifications exécutées

Sur Python 3.12 : `MOCK_MODE=true .venv/bin/python -m pytest -q` : **21 réussis**.
`pip check`, `compileall -q company tests`, `git diff --check` : réussis.
Tests : réunion, budgets, concurrence de réservations SQLite, réservations conservées
après timeout, secrets absents des erreurs, échec global sans tâches, écriture de
rapport échouée, approbation obligatoire, traversal/symlinks, réseau interdit en
simulation, adaptateurs SDK factices et repli minigraph.

La CI distante et Python 3.11 ne sont pas présentés comme exécutés localement.
Aucune API payante appelée. `.env`, bases et rapports restent ignorés par Git.

## Limites et critères non satisfaits

Le budget reste une prévision locale, jamais un plafond bancaire : dépassements
réels, tokens de raisonnement, tarifs incorrects, taxes et conversion restent
possibles. Le stockage Float peut produire des arrondis. Le verrou empêche deux
réservations concurrentes de lire le même solde, pas les dépenses hors application.
PostgreSQL et son verrou nécessitent une validation d'intégration distincte.
Une régularisation concurrente du même identifiant n'est pas couverte ici.
Le critère de budget strict absolu n'est donc pas satisfait.

Pas de verrou global du scheduler ni de claim atomique de prototype ; conserver
une seule instance. Une course filesystem hostile reste possible entre contrôle
et écriture : ne pas partager le répertoire avec un processus non fiable.
Un rapport peut devenir orphelin si la transaction DB échoue après son écriture.
Le routage peut mélanger historique mock et réel ; pas de benchmark indépendant.
Les timeouts HTTP ne garantissent pas un délai global de workflow. Pas de retry
applicatif ni checkpoints persistants dans ce lot.

## Lot 2 recommandé

1. Introduire migrations Alembic et montants monétaires exacts ; tester PostgreSQL
   et ses verrous, puis sauvegarde/restauration.
2. Ajouter une file persistante avec claim atomique, clés d'idempotence et transitions
   d'état contrôlées pour réunions, prototypes et régularisations.
3. Ajouter checkpoints LangGraph et reprise sans répéter un appel possiblement facturé.
4. Séparer données mock/réelles et événements d'audit ; définir la réconciliation
   des coûts inconnus avant toute politique de retries bornés.
5. Tester crash/reprise, concurrence multi-processus et arrêt du planificateur.

## Livraison et accès distant

La branche `feat/lot-1-audit` est poussée sur origin. Un cycle CLI `init`, `once`,
`status` sur une base SQLite temporaire s'est terminé avec rapport, deux tâches
`pending_review` et coût nul. L'arbre de travail est propre après commit.
La création de PR par `gh pr create --draft` est bloquée :
`POST https://api.github.com/graphql` renvoie `Forbidden`, tout comme une lecture
REST du dépôt. La lecture et l'écriture Git via le proxy fonctionnent ; elles
ne prouvent pas l'accès API. Aucun jeton supplémentaire demandé ou extrait.
La description de PR est conservée dans `/workspace/lot-1-pr.md`.
Pour terminer la livraison, rétablir l'accès API GitHub de cet environnement ou
créer manuellement une PR **de brouillon** depuis la branche poussée, puis vérifier
la matrice CI distante avant fusion. Aucune PR créée ou fusionnée à ce stade.
