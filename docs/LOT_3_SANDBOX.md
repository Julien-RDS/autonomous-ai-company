# Lot 3 : tests isolés des prototypes dans Docker

Branche `feat/lot-3-sandbox`, dérivée de `main` (83e2710, PR #1 à #3 fusionnées).
Aucun appel API payant, aucun worker réel, aucun prototype produit par une IA n'a
été exécuté pendant ce chantier : les tests Docker utilisent des fixtures écrites
par les développeurs (`tests/test_sandbox.py`).

## Revue préalable du socle (priorité 1)

| Point examiné | Constat | Suite donnée |
|---|---|---|
| Secrets dans les logs | `repr(Settings)` contenait clés API et mot de passe PostgreSQL | Corrigé : champs `repr=False`, test de régression |
| Ancien worker après perte de lease | Si l'ancien processus vit encore et garde le verrou du checkpoint ou du prototype, chaque tentative du remplaçant échouait : passage en `FAILED` sans vraie panne, cycle jamais terminé | Corrigé : exception `Busy`, `Queue.defer_busy` ne consomme pas `max_attempts`, au plus 10 reports puis `WAITING_APPROVAL` (humain) |
| Traçabilité des approbations | Acteur toujours `local-cli` | `approve --actor` validé (défaut `cli:<utilisateur>`) |
| Chemins, symlinks, publication / SQL | Validation `safe_files`, staging, renommage, vérification du manifeste sous lease : pas de défaut reproduit | Inchangé |
| Réservations à coût incertain | Conservées (`unknown`) jusqu'au rapprochement humain | Inchangé |
| Migrations sur base existante | Bases SQLite et PostgreSQL créées par `main` avec données : 005 → 006 sans perte | Migration 006 purement additive |
| Image Docker du worker | Seuls `company/`, `migrations/`, `alembic.ini` copiés ; `.env` exclu | Inchangé |

## Principe

Deux approbations humaines distinctes :

1. `company approve TASK` autorise la **génération** de fichiers (inchangé). Elle
   n'autorise **jamais** l'exécution.
2. `company sandbox approve TASK --reviewed` autorise **une seule** exécution isolée,
   liée au SHA-256 exact du manifeste, au profil de test et à l'ID immuable de l'image.
   Si le contenu ou l'image autorisée change, l'autorisation devient inutilisable.
   Une exécution consomme l'autorisation, même si elle échoue ou est interrompue :
   rien n'est rejoué automatiquement.

## Configuration

```bash
# Image explicitement autorisée, référencée par son ID immuable (jamais par un tag) :
docker pull python:3.12-slim          # action humaine, réseau de l'hôte
docker image inspect --format '{{.Id}}' python:3.12-slim
# Copier l'ID dans .env :
SANDBOX_PYTHON_IMAGE_ID=sha256:<64 caractères hexadécimaux>
# Facultatif : répertoire hôte des espaces temporaires (défaut /tmp/aac-sandbox)
SANDBOX_WORKDIR=/chemin/sans/virgule
```

Sans `SANDBOX_PYTHON_IMAGE_ID`, toute approbation d'exécution est refusée.
L'exécuteur utilise `--pull never` : une image absente provoque une erreur, jamais
un téléchargement. Docker Engine doit être installé sur l'**hôte** qui lance la CLI.

## Commandes

```bash
python -m company approve TASK --actor julien          # génération
python -m company prototype TASK                        # ou via la file durable
# Relire les fichiers dans artifacts/prototypes/TASK/ puis :
python -m company sandbox approve TASK --reviewed --actor julien --reason "code relu"
python -m company sandbox run TASK                      # une exécution, résultat persisté
python -m company sandbox status TASK                   # historique des tentatives
python -m company sandbox cleanup                       # orphelins échus
python -m company sandbox cleanup --all                 # tout, si aucune exécution en cours
```

Profil disponible : `python-unittest`, qui lance
`python -I -B -m unittest discover -s /workspace -t /workspace -p test_*.py -v`.
La commande est fixée par l'application ; aucun contenu fourni par un modèle n'entre
dans les arguments, et aucun shell n'est utilisé. Un prototype sans fichier
`test_*.py` aboutit à `FAILED` (code 5 : aucun test exécuté).

## Isolation appliquée

| Mesure | Option Docker |
|---|---|
| Réseau coupé (loopback seul) | `--network none` |
| Utilisateur non root | `--user 65534:65534` |
| Capacités Linux supprimées | `--cap-drop ALL` |
| Privilèges supplémentaires interdits | `--security-opt no-new-privileges:true` (seccomp par défaut conservé) |
| Racine en lecture seule | `--read-only` |
| Écritures temporaires isolées | `--tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m` |
| Code monté en lecture seule | bind d'un répertoire neuf, fichiers 0444, `readonly` |
| Mémoire, swap, CPU, processus | `--memory 256m --memory-swap 256m --cpus 1 --pids-limit 64` |
| Descripteurs, core dumps | `--ulimit nofile=256:256 --ulimit core=0` |
| IPC, journaux hôte | `--ipc none --log-driver none` (aucun fichier de log illimité sur l'hôte) |
| Image identifiée | lancement par `sha256:<ID>`, `--pull never` |
| Aucun secret | aucune variable de l'hôte transmise ; seules `HOME`, `LANG`, `PYTHONDONTWRITEBYTECODE` |
| Aucun socket Docker, aucun répertoire sensible | seul l'espace temporaire est monté |
| Durée | SIGKILL à l'échéance (60 s par défaut), même si SIGTERM est ignoré |
| Sorties | 64 Kio conservés par flux, arrêt au-delà de 1 Mio observé, octets NUL neutralisés |
| Nettoyage | `docker rm -f` et suppression de l'espace après succès, échec, timeout ou erreur ; label `aac.sandbox` et échéance pour les orphelins |

États persistés dans `sandbox_runs` : `RUNNING`, `PASSED`, `FAILED`, `TIMEOUT`,
`OUTPUT_LIMIT`, `OOM`, `ERROR`, `INTERRUPTED`. Chaque ligne est rattachée à la tâche,
à son numéro de tentative, à l'autorisation consommée, à l'image et au contenu.
Les sorties stockées sont des données non fiables.

## Limites concrètes

- Docker n'est pas une frontière de sécurité absolue : le noyau est partagé ; une
  faille noyau, runc ou du démon peut permettre une évasion. Pour du code hostile,
  préférer une machine virtuelle jetable ou gVisor/Kata (non implémenté).
- La CLI sandbox pilote le démon Docker, équivalent à root sur l'hôte. Elle doit
  être lancée par un humain sur l'hôte. Ne **jamais** monter le socket Docker dans
  le worker Compose ni dans un conteneur.
- Un conteneur orphelin (CLI tuée en cours d'exécution) continue jusqu'au prochain
  `sandbox cleanup` ; `sandbox run` nettoie d'abord les orphelins échus.
- Les limites cgroups dépendent de l'hôte (cgroup v1/v2, Docker rootless, WSL).
  Elles ont été vérifiées ici sur Docker 29.1 en cgroup v1.
- `--cpus` limite le débit, pas l'usage total ; le temps est borné par le timeout.
- Le tmpfs `/tmp` est compté dans la mémoire du conteneur.
- L'exécution teste le contenu du manifeste en base, celui qui a été approuvé,
  pas les éventuelles modifications manuelles des fichiers dans `artifacts/`.
- L'image de test de la CI est tirée par tag (`python:3.12-slim`) ; son ID est
  résolu et enregistré pendant le test. Aucun digest n'a été inventé : épinglez
  un digest vérifié si vous voulez une CI reproductible.
- Un seul profil (`python-unittest`) ; pas encore de profil HTML/JS ni d'exécution
  depuis la file durable (exécution CLI attendue par un humain).

## Validation observée dans l'environnement de développement

Python 3.12 et 3.11, PostgreSQL 16 réel, Docker Engine 29.1 (cgroup v1), image de
test construite localement (Docker Hub inaccessible depuis cet environnement) :
**95 tests réussis, 12 ignorés, 0 échec**. Ignorés : 11 variantes SQLite de tests
réservés à PostgreSQL, et l'arrêt/redémarrage du serveur PostgreSQL (nécessite
`TEST_PG_CONTAINER`, couvert par la CI). Contre-épreuve : le test réseau échoue
bien lorsque `--network none` est retiré volontairement.

```bash
MOCK_MODE=true \
TEST_DATABASE_URL=postgresql+psycopg://company:MOT_DE_PASSE@127.0.0.1:5432/company_test \
TEST_SANDBOX_IMAGE=python:3.12-slim \
python -m pytest -q -rs
```
