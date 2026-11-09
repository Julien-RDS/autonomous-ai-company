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

## Suite après revue (novembre 2026)

- Remise en file humaine :
  `python -m company.worker requeue WORK_ID --actor julien --reason "cause corrigée"`.
  Réservé aux travaux MOCK non externes en FAILED/WAITING_APPROVAL, sans lease.
  La migration 007 conserve les revues humaines et tout l'historique des tentatives.
  La nouvelle revue ouvre un budget de tentatives et de reports BUSY neuf.
  Cette commande ne crée aucune approbation d'exécution sandbox.
- Profil `static-html` : vérifie index.html, éléments html/body/title et ressources
  locales existantes. Refuse les URL externes et la traversée de chemins. Le JS
  n'est jamais interprété ; ce contrôle structurel ne valide pas le comportement
  d'un navigateur ni la sécurité complète d'une application web.
  Exemple : `python -m company sandbox approve TASK --profile static-html
  --actor julien --reason "structure relue" --reviewed`, puis `sandbox run TASK`.
- `/api/sandbox-runs` et le dashboard affichent les 50 dernières exécutions en
  lecture seule. Les sorties stdout/stderr ne sont pas diffusées par cette API.
- La variable de dépôt GitHub `SANDBOX_TEST_IMAGE` permet de choisir explicitement
  l'image de CI. Le défaut est un digest Python vérifié dans les métadonnées Docker
  du registre le 9 novembre 2026, jamais un digest inventé. Après changement,
  vérifier la provenance de l'image et réobserver la CI.
- Un run unittest sans aucun test est un échec. Le point d'entrée de l'image est
  remplacé par la commande Python fixe ; les variables de proxy injectables par
  Docker sont vidées dans le conteneur. Le client Docker garde sa configuration.
- Les répertoires et conteneurs préexistants ne sont pas nettoyés par une tentative
  qui ne les a pas créés. Le nettoyage d'orphelins reste une commande dédiée.

## Limite observée dans l'environnement cloud

Le démon actuel annonce cgroup v2, mais MemoryLimit, SwapLimit, PidsLimit sont
faux : Docker rootless sans délégation cgroup. Lors du test initial du bundle,
une allocation supérieure à 64 Mo a réussi malgré `--memory 64m`.
Ce résultat est un échec de la garantie d'isolation, pas un test réussi.

Le lanceur vérifie désormais les capacités mémoire, swap, PID et quota CPU et
refuse toute exécution si elles sont indisponibles. Ne pas contourner ce refus.
La validation des tests Docker complets exige un hôte avec contrôleurs cgroup
opérationnels (par exemple un runner GitHub compatible). Aucun worker réel ni
prototype IA n'a été exécuté pendant cette revue.

Résultats de revue du 9 novembre 2026 :
- Bundle original, Python 3.12 + PostgreSQL + Docker : 95 réussis, 11 ignorés,
  1 échec (limite mémoire inopérante).
- Après corrections, Python 3.12 + PostgreSQL : 96 réussis, 21 ignorés.
- Après corrections, Python 3.11 + PostgreSQL dans Docker : 95 réussis, 22 ignorés.
Les tests d'exécution Docker sont explicitement non validés dans ces deux derniers
passages (TEST_SANDBOX_IMAGE non défini) : le garde-fou réel a été testé séparément
et refuse correctement le démon sans cgroups. Le passage 3.12 inclut l'arrêt et
redémarrage PostgreSQL ; le passage 3.11 ne le lance pas depuis le conteneur.
La CI se déclenche aussi sur push de feat/lot-3-sandbox pour pouvoir vérifier
l'isolation sur un runner compatible avant la création de PR. Aucun résultat
GitHub distant n'est affirmé sans observation.

## Validation réelle et revue critique (9 novembre 2026, suite)

### CI distante observée

Run « Tests » n° 8 (`42589954340`) sur `706731e` : **terminé avec succès**, jobs
`tests (3.11)` et `tests (3.12)`. Les journaux exigent une authentification GitHub et
n'ont pas pu être lus anonymement : le décompte réussis/ignorés n'est pas observé.
Inférence (non observation) : `TEST_SANDBOX_IMAGE` est défini en CI et la fixture
`image_id` échoue (sans skip) si l'image manque ; un refus du sandbox aurait fait
échouer les tests attendant `PASSED`. Un run vert implique donc très probablement
que les tests Docker ont été exécutés sur le runner GitHub.

### Hôte de validation

Docker Engine 29.1 **rootful**, cgroup v1 (cgroupfs), 1 CPU ; PostgreSQL 16 réel ;
image de test Python 3.12 construite localement (Docker Hub inaccessible depuis cet
environnement). Les limites ont été **mesurées dans le conteneur**, pas seulement
déclarées : `memory.limit_in_bytes` = 268435456, `memsw` identique (pas de swap),
`pids.max` = 64 (fork bloqué après 62 enfants), quota CPU 50000/100000 avec 4
processus saturants mesurés à 0,49 CPU, dépassement mémoire tué (OOM, code 137).

### Défauts reproduits puis corrigés (tests de régression)

| # | Défaut reproduit | Correction |
|---|---|---|
| A | `static-html` acceptait 7 types de ressources externes sur 8 (srcset, meta refresh, action de formulaire, `url()` CSS, `@import`, `<object data>`) et n'analysait que `index.html` | Toutes les pages HTML et feuilles CSS ; attributs d'URL, `srcset`, `style`, `<style>`, meta refresh ; `<base>` et liens symboliques refusés |
| B | La base des espaces de travail suivait un lien symbolique et acceptait un répertoire `0777` (`/tmp` partagé) | Répertoire réel exigé, sans lien, appartenant à l'utilisateur, droits `0700` ; refus avant tout appel Docker |
| C | La CLI affichait les sorties non fiables brutes (séquences ANSI/OSC, inversion bidi) | `display_safe` neutralise les caractères de contrôle à l'affichage ; les données stockées restent intactes |
| D | L'approbation portait sur le manifeste en base alors que l'humain relit les fichiers sur disque | Approbation refusée si les fichiers relus diffèrent du contenu qui serait exécuté, ou sont absents ; le SHA-256 autorisé est affiché |
| E | Sur un démon incapable d'imposer les limites, le refus consommait l'approbation | Contrôle préalable (base, démon, capacités) **avant** consommation |
| F | Un résultat arrivé après un nettoyage forcé était jeté | Résultat tardif conservé, état `INTERRUPTED` maintenu, audit `sandbox_late_result` |
| G | Un conteneur encore `created` (démarrage) était classé orphelin et nettoyable | `created`/`running`/`restarting` avant échéance = actif ; nettoyage refusé |

En plus des capacités déclarées par `docker info`, chaque exécution lance d'abord un
**conteneur sonde** fixe (code de l'application, mêmes options) qui lit les valeurs
cgroup effectives. Si mémoire, swap, PID, quota CPU ou utilisateur non root ne
correspondent pas exactement à la demande, l'exécution est refusée (`ERROR`) avant
que le moindre test soit lancé. Un test retire volontairement `--pids-limit` et
vérifie ce refus. Coût : un conteneur supplémentaire d'environ une demi-seconde.

### Diagnostic et récupération

```bash
python -m company sandbox doctor                      # lecture seule
python -m company sandbox status TASK                 # affiche les identifiants run=
python -m company sandbox cleanup --run RUN_ID --actor julien --reason "parent tué"
python -m company sandbox cleanup                     # orphelins échus, audités
```

`doctor` affiche la joignabilité du démon, la version cgroup, le mode rootless, les
capacités manquantes, chaque run `RUNNING` (actif, orphelin : conteneur absent,
arrêté ou échéance dépassée) et les conteneurs sandbox sans run connu.

`cleanup --run` ne touche que le conteneur, la sonde et l'espace de travail de ce run.
Il refuse tant que le conteneur est actif avant son échéance (60 s + 120 s de grâce).
Chaque décision, y compris un refus, est enregistrée dans `sandbox_cleanups`
(migration 008 : acteur, motif, état précédent, résultat, conteneur et espace
supprimés) et dans `audit_events`. Aucun nettoyage ne consomme d'approbation ni ne
relance de test : un run interrompu reste interrompu, une nouvelle exécution exige
une nouvelle approbation.

### Résultats exacts

Python 3.12.3 et 3.11.18, PostgreSQL 16 réel, Docker réel, `TEST_SANDBOX_IMAGE` défini :
**150 réussis, 12 ignorés, 0 échec** pour chaque version. Ignorés : 11 variantes
SQLite de tests réservés à PostgreSQL ; 1 arrêt/redémarrage du serveur PostgreSQL
(`TEST_PG_CONTAINER` non disponible ici). Aucun conteneur sandbox restant après la
suite. Validés en conteneur réel : succès, échec, zéro test découvert, mémoire, PID,
CPU, timeout avec SIGTERM ignoré, réseau, secrets, socket Docker, lecture seule,
non-root, sorties bornées, erreur Docker, interruption (`os._exit`) et `SIGKILL` du
parent puis nettoyage ciblé, deux exécutions concurrentes isolées, profil `static-html`.

### Limites restantes

- Validation faite en cgroup v1 rootful ; en cgroup v2 et rootless avec délégation,
  la sonde lit `memory.max`, `memory.swap.max`, `pids.max`, `cpu.max` : chemin
  couvert par des tests unitaires, non exécuté ici sur un vrai hôte cgroup v2.
- Docker reste une isolation partielle (noyau partagé, démon root). La CLI pilote
  un démon équivalent à root : à lancer par un humain sur l'hôte.
- `static-html` est structurel : les URL construites par JavaScript à l'exécution,
  les attributs exotiques non listés et les failles XSS ne sont pas détectés.
- La sonde et l'exécution sont deux conteneurs successifs : un changement de
  configuration du démon entre les deux n'est pas exclu (fenêtre d'une seconde).
- Le digest d'image par défaut de la CI n'a pas pu être revérifié ici (registre
  inaccessible).
