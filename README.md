# Autonomous AI Company — prototype exécutable

**Objectif** : un conseil d'agents OpenAI, Anthropic et Google qui propose des opportunités d'activité, les critique, vote, enregistre la décision et prépare des tâches testables. Le système continue automatiquement **si un planificateur reste lancé**.

**Niveau livré : MVP opérationnel, pas entreprise déjà rentable.** La démonstration tourne sans compte ni carte bancaire. La connexion aux 3 IA est réelle une fois les clés, modèles et prix configurés ; les API ne peuvent pas être appelées depuis l'archive sans cela.

## Fonctionnalités présentes

| Fonction | Statut | Limites |
|---|---|---|
| Propositions / critique contradictoire / votes des 3 agents | ✅ | Vote et opportunités simulés en mode démo ; les agents peuvent se tromper |
| Orchestration LangGraph | ✅ | Cycle linéaire borné, pas de libre arbitre / évaluation objective automatique |
| Mémoire SQLite ou PostgreSQL | ✅ | Historique de réunions, réponses, consommations et tâches ; reprise sur nouveau cycle après arrêt |
| Recherche web Tavily | Facultative | Une requête par proposition, uniquement avec clé et tarif paramétré ; résultats non vérifiés |
| Budget / facturation | ✅ estimation locale | Ne remplace **pas** les plafonds opérateur, taxes, révisions tarifaires ou rapprochement factures |
| Auto-attribution expérimentale | ✅ limitée | Routage sur validations humaines consignées ; sans résultats, rotation entre modèles |
| Génération de fichiers de prototypes | ✅ sous validation | Pas d'exécution automatique du code généré |
| Tests isolés des prototypes (Docker) | ✅ sous approbation dédiée | Une approbation d'exécution par lancement, lancé par un humain sur l'hôte ; Docker n'est pas une isolation absolue |
| Tâches et résultats terrain | ✅ | Approbation et saisie humaine explicites |
| Planification quotidienne | ✅ | Processus actif indispensable ; une seule instance prévue |
| Dashboard web local | ✅ | Lecture seule, sans authentification, sur 127.0.0.1 uniquement |
| GitHub auto-commit, navigateur autonome, emails clients, paiements, contrats, déploiements publics, mesure de revenus | ❌ | Non implémentés pour ce MVP, ne pas les supposer actifs |

## Démarrage sous Ubuntu ou WSL

Pré-requis : Python **3.11+**, `python3-venv`, `pip`, éventuellement Docker Desktop/Engine pour PostgreSQL. Depuis le dossier extrait :

```bash
cd autonomous-ai-company
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
python -m company init
python -m company once
python -m company status
```

Vous obtiendrez un compte rendu `reports/<ID>.md`, 9 réponses d'agents fictifs (3 propositions + 3 critiques + 3 votes), 2 tâches en attente et **aucune** dépense API.

### Tableau de bord

```bash
python -m company dashboard
```

Ouvrir **http://127.0.0.1:8000** dans le navigateur local. Aucun bouton ne déclenche de paiement ou ne modifie une tâche. Vue JSON : `/api/status`.

### Générer un prototype local après validation

```bash
python -m company status
# Recopier l'identifiant COMPLET d'une tâche dont la catégorie est 'prototype'
python -m company approve IDENTIFIANT_TACHE
python -m company prototype IDENTIFIANT_TACHE
# Les fichiers apparaissent dans artifacts/prototypes/IDENTIFIANT_TACHE/
```

**Important** : génération de fichiers uniquement, **pas** d'exécution, ni d'installation de dépendances, ni de publication. Examiner tout code avant de l'ouvrir ou de le faire tourner. Pour lancer ses tests, utiliser uniquement le sandbox Docker du Lot 3, avec une approbation d'exécution distincte (voir plus bas).

### Enregistrer une évaluation après un vrai test

```bash
python -m company approve IDENTIFIANT_TACHE_VALIDATION
python -m company measure IDENTIFIANT_TACHE_VALIDATION --success 1 --notes "5 entretiens, 3 intéressés"
python -m company measure IDENTIFIANT_TACHE_PROTOTYPE --success 1 --notes "Critères fonctionnels vérifiés manuellement"
python -m company ranking
```

Le routage n'attribue un avantage qualitatif à un fournisseur qu'après au moins 3 évaluations de prototypes pour ce fournisseur. **Les votes du débat et les textes IA ne servent pas d'évaluation objective de qualité.** L'outil ne comptabilise pas automatiquement de vrais contrats ou revenus. Le champ `--value-eur` représente une valeur consignée par un humain, pas un paiement réel.

## Connecter les trois véritables IA

Éditer `.env` :

1. `MOCK_MODE=false`.
2. Ajouter `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`.
3. Ajouter `OPENAI_MODEL`, `ANTHROPIC_MODEL`, `GEMINI_MODEL` : **identifiants API exacts disponibles sur vos comptes** (différents des abonnements web).
4. Compléter les six tarifs `*_INPUT_EUR_PER_MTOK` et `*_OUTPUT_EUR_PER_MTOK`, **en EUR par million de tokens**, aux tarifs en vigueur (convertir si fournisseur facture en USD). Tant que les 6 champs restent vides, le programme REFUSE le mode réel. Prévoir le traitement du cache et des tokens de raisonnement dans votre budget.
5. Vérifier les limites : `MONTHLY_BUDGET_EUR=100`, `MAX_RUN_BUDGET_EUR=3`, `MAX_OUTPUT_TOKENS=500`.
6. Activer **séparément les plafonds/alertes de dépenses chez chacun des trois fournisseurs**, car la prévision et la facturation locale ne garantissent pas un plafond bancaire strict. Éviter les exécutions simultanées.
7. Puis `python -m company once`.

**Tarification et dépenses** : la réservation préalable se base sur une approximation prudente de la taille du prompt et sur la limite de tokens générés. L'estimation peut se tromper et certains frais ne sont pas représentés (recherches, taxes, conversion, prompts mis en cache, frais spécifiques aux modèles). Les appels échoués gardent leur réservation budgétaire comme coût potentiellement encouru. Les erreurs sont enregistrées ; pas de répétitions infinies. **Le système ne permet pas de garantir 100 €/mois facturés.**

Liens de référence :
- [OpenAI Responses API](https://developers.openai.com/api/reference/python/resources/responses/methods/create)
- [Anthropic Messages API](https://platform.claude.com/docs/en/api/python/messages/create)
- [Google Gen AI Python](https://googleapis.github.io/python-genai/)
- [LangGraph](https://reference.langchain.com/python/langgraph/graph/state/StateGraph)

### Recherche web réelle (facultative)

Ajouter à `.env` :

```dotenv
TAVILY_API_KEY=VOTRE_CLE
TAVILY_COST_EUR_PER_SEARCH=VOTRE_COUT_PAR_REQUETE_EN_EUR
```

Lors d'une réunion réelle, jusqu'à **3 recherches** seront faites et comptabilisées au tarif indiqué. Les liens et extraits apparaissent dans le rapport ; les extraits sont des données non fiables. Désactiver en laissant la clé vide. Les prix du fournisseur de recherche sont à vérifier avant activation.

## PostgreSQL (facultatif)

Pour utiliser PostgreSQL à la place de SQLite :

```bash
# Dans .env : définir POSTGRES_PASSWORD avec un mot de passe choisi
# Dans .env : DATABASE_URL=postgresql+psycopg://company:MOT_DE_PASSE@127.0.0.1:5432/company
# Les caractères spéciaux du mot de passe doivent être encodés dans l'URL

docker compose up -d postgres
python -m company init
```

Le service Postgres expose le port uniquement sur l'interface locale ; volume persistant `postgres_data`. Pour la démo et l'apprentissage, SQLite suffit. Ne pas publier le port sur internet. Les migrations sont versionnées avec Alembic ; voir le guide du Lot 2.

## Réunions quotidiennes autonomes

Configurer `SCHEDULE_HOUR=9`, `SCHEDULE_MINUTE=0`, `TIMEZONE=Europe/Paris` puis lancer :

```bash
python -m company scheduler
```

Un cycle est lancé à l'heure programmée tant que le processus tourne. Pour du 24 h / 24, l'exécuter sous un service systemd, Docker ou serveur dédié avec redémarrage, sauvegardes et journaux. **Ne pas lancer plusieurs planificateurs** ; le scheduler ne dispose pas de verrou global. Les réservations budgétaires sont transactionnelles.

## Architecture

```text
company/
  __main__.py     CLI : init, once, status, scheduler, dashboard, approve, prototype, measure, ranking
  config.py       Validation .env, mode de fonctionnement et limites
  database.py     SQLAlchemy : réunions, messages, dépenses, tâches
  budget.py       Réservation / relevé des consommations
  agents.py       API OpenAI, Claude, Gemini ; adaptateurs mock
  workflow.py     Graph LangGraph : propose -> research -> critique -> vote -> decide
  research.py     Tavily (facultatif)
  routing.py      Sélection basée sur retours humains et métriques observées
  prototyping.py  Écriture de fichiers autorisés uniquement
  dashboard.py    Vue FastAPI locale (lecture seule)
tests/             Tests unitaires et d'intégration du mode démo
reports/           Comptes rendus générés
artifacts/         Prototypes générés
```

### Politique d'autonomie

Le programme est autorisé à produire des propositions, critiquer, voter, rechercher (si la clé est renseignée), créer des tickets internes et, après approbation, écrire des fichiers de prototype. Il **ne peut pas** dépenser hors facturation des API activées, envoyer des emails, prospecter, signer, acheter, se connecter à un compte bancaire, faire un `git push` ni exécuter des scripts créés par un modèle. Les actions sensibles nécessiteront des connecteurs dédiés et un système d'autorisations explicites avant extension du projet.

L'amélioration n'est pas une capacité auto-magique : les performances métier doivent être mesurées à partir de données indépendantes. La comparaison automatique de fiabilité et de vitesse est préparée ; le routage actuel favorise l'exploration tant qu'il ne dispose pas de suffisamment de résultats humains.

## Tests

```bash
python -m pytest -q
```

Aucune API payante ne sera contactée par ces tests.

### Test hors ligne sans installer LangGraph

Le mode démonstration utilise automatiquement un minigraphe séquentiel (`company/minigraph.py`) lorsque la dépendance `langgraph` est absente. **Ce repli ne remplace pas LangGraph**, notamment sa persistance de graphes, ses interruptions et son parallélisme. Pour utiliser le moteur officiel et les API réelles, installer toutes les dépendances avec `pip install -r requirements.txt`.

## Installation simplifiée

Sous Ubuntu/WSL, `bash setup.sh` installe les dépendances et prépare `.env` en mode simulation. Pour la liste exacte des éléments livrés et de ce qui reste à développer, lire [docs/AVANCEMENT.md](docs/AVANCEMENT.md). Un exemple de rapport est fourni dans `examples/compte_rendu_demo.md` et le cahier des charges original dans `docs/cahier_des_charges_original.txt`.

## Contribution et relais Claude Code

Voir [`CLAUDE.md`](CLAUDE.md) et [`docs/CLAUDE_HANDOFF.md`](docs/CLAUDE_HANDOFF.md). La CI GitHub exécute les tests en mode simulation, sans clés API.

### Publier ce dossier dans un dépôt GitHub privé

Après avoir créé un dépôt GitHub **vide** nommé `autonomous-ai-company` (ne pas cocher README ni licence), depuis ce dossier :

```bash
git init -b main
git add .
git commit -m "chore: bootstrap autonomous AI company MVP"
git remote add origin git@github.com:VOTRE_COMPTE/autonomous-ai-company.git
git push -u origin main
```

Utiliser `gh repo create autonomous-ai-company --private --source=. --remote=origin --push` si GitHub CLI est configuré, à la place des deux dernières commandes; ne pas exécuter les deux méthodes à la fois.

## Persistance et reprise (Lot 2)

PostgreSQL 16, migrations Alembic, montants Decimal, file durable avec leases,
checkpoints LangGraph et approbations persistantes sont disponibles.
Le worker durable fonctionne en simulation. Démarrage : `docker compose up --build -d`
après configuration de POSTGRES_PASSWORD dans `.env`.
Les commandes, migrations, sauvegardes, tests de crash et limites sont détaillés
dans [docs/LOT_2_PERSISTENCE.md](docs/LOT_2_PERSISTENCE.md).

### Prototypes dans la file durable

Après `python -m company approve IDENTIFIANT_TACHE_PROTOTYPE`, soumettre avec
`python -m company.worker enqueue-prototype IDENTIFIANT_TACHE_PROTOTYPE`.
Le worker MOCK reprend une génération interrompue et conserve les fichiers déjà
publiés. Voir [la revue et les commandes de rapprochement des coûts](docs/REVIEW_AND_DURABLE_PROTOTYPES.md).

## Tests isolés des prototypes (Lot 3)

Après génération et relecture, une **approbation d'exécution distincte et à usage
unique** permet de lancer les tests `test_*.py` d'un prototype dans un conteneur
Docker sans réseau, non root, en lecture seule et borné en durée, mémoire, CPU,
processus et sortie :

```bash
# .env : SANDBOX_PYTHON_IMAGE_ID=sha256:... (ID vérifié d'une image locale)
python -m company sandbox approve IDENTIFIANT_TACHE --reviewed --actor VOTRE_NOM --reason "code relu"
python -m company sandbox run IDENTIFIANT_TACHE
python -m company sandbox status IDENTIFIANT_TACHE
python -m company sandbox doctor            # capacités Docker et runs orphelins
python -m company sandbox cleanup --run RUN_ID --actor VOTRE_NOM --reason "motif"
```

Le code généré n'est jamais exécuté sur l'hôte. Configuration, garanties et
limites : [docs/LOT_3_SANDBOX.md](docs/LOT_3_SANDBOX.md).

Les résumés de tests déclarés et leur provenance sont visibles dans le dashboard.
La récupération confirme la disparition des conteneurs avant suppression des
workspaces. Voir [résultats et récupération](docs/SANDBOX_RESULTS_RECOVERY.md).

## Supervision locale (lecture seule)

`python -m company dashboard` puis http://127.0.0.1:8000 : vue d'ensemble, réunions et
décisions, tâches, file, exécutions sandbox et budget. Aucune action depuis le web : les
interventions nécessaires sont indiquées sous forme de commandes CLI avec leur effet.
Détails : [docs/SUPERVISION.md](docs/SUPERVISION.md).
