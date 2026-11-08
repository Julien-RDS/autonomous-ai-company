# Revue des Lots 1/2 et prototypes durables

## Livraisons et accès GitHub

État vérifié dans l'historique distant le 8 novembre 2026 :
- Lot 1 intégré dans main par la PR #1 (merge 2105d2a).
- Lot 2 intégré dans main par la PR #2 (merge fe5e1ab).
- Complément : feat/durable-prototypes, désormais synchronisée avec main et à
  proposer contre main. Ces fusions ont été constatées, pas exécutées par cette reprise.

La lecture et l'écriture Git fonctionnent. L'API est bloquée avant authentification :
CONNECT vers api.github.com retourne 403 du proxy Envoy ; le domaine est absent
de la liste réseau autorisée. Ajouter **api.github.com** aux paramètres réseau
de l'environnement en préservant les domaines existants. Les outils de lecture
du brouillon ne permettent pas actuellement de vérifier cette configuration.
Aucune clé GitHub supplémentaire n'a été demandée ou extraite.
Après modification, retester l'API, créer la PR complémentaire et observer la CI.
Les anciennes descriptions de livraison des Lots 1/2 sont historiques ; les commits
de fusion confirment leur intégration sans établir le résultat de leurs checks CI.

Ni PR ni CI distante ne sont déclarées réussies sans réponse GitHub.

## Revue ciblée et corrections

1. Le cycle durable ne couvrait pas les prototypes. WorkItem porte désormais un
   `kind` et une référence unique au ticket prototype. L'unicité historique du run
   laisse place à l'unicité du ticket ; les données et tentatives sont conservées.
2. Statut `approved` seul insuffisant : génération et soumission exigent désormais
   une ligne Approval. Un ancien ticket `approved` peut être reconfirmé explicitement
   avec la commande approve pour créer sa preuve de validation humaine.
3. Un crash entre écriture de fichiers et validation SQL empêchait la reprise :
   manifeste durable avant publication, fournisseur fixé, staging privé, renommage
   du répertoire en bloc, vérification exacte des fichiers après redémarrage.
4. Le verrou PostgreSQL par prototype est commun au worker et à la CLI directe.
   SQLite emploie flock. Le lease est vérifié avant publication et validation SQL.
5. Les réponses sauvegardées étaient tronquées à 20 000 caractères, rendant un
   cache JSON potentiellement illisible après crash. Les réponses complètes sont
   maintenant conservées pour permettre la reprise.
6. Une régularisation en erreur avec usage partiel pouvait libérer une partie du
   budget incertain. Elle conserve désormais le maximum de la réservation et du
   coût observé jusqu'au rapprochement humain.
7. Les migrations SQLite reconstruisant une table référencée sont vérifiées sur
   une file remplie : contrôle intégral des relations avant commit et réactivation
   des foreign keys sur la connexion. Arrêter les écrivains avant migration.

## Utilisation

```bash
python -m pip install -r requirements.txt
alembic upgrade head
# Après une réunion MOCK terminée :
python -m company approve IDENTIFIANT_TACHE_PROTOTYPE
python -m company.worker enqueue-prototype IDENTIFIANT_TACHE_PROTOTYPE
python -m company.worker run --once
```

`run --once` traite le prochain travail éligible, pas nécessairement le prototype
si d'autres travaux sont déjà dans la file. Une soumission répétée retourne le
même travail. Aucun code généré n'est exécuté. Le mode réel de la file est refusé.
La commande directe `company prototype` partage le verrou et la reprise.
Les artefacts ont un volume Compose persistant dédié.

Après vérification indépendante de la facturation par un humain :

```bash
python -m company reconcile-cost IDENTIFIANT_LEDGER --amount-eur 0.02 --reference FACTURE_OU_DOSSIER
```

Seules les lignes `unknown` sont rapprochables. La référence est obligatoire,
conservée avec le montant et un événement d'audit. Répéter la même confirmation
est idempotent ; une confirmation différente est refusée. La clé d'opération
reste occupée : cette commande ne relance jamais l'appel fournisseur.
Ne pas mettre de secret dans la référence. Aucun plafond bancaire garanti.

## Vérifications et limites

Tests SQLite et PostgreSQL réel : approbation obligatoire, déduplication, verrou
partagé, fichiers inchangés au rejeu, refus d'écraser une édition humaine, échec
avant publication, processus tué après publication puis reprise, migration d'une
file SQLite peuplée, rapprochement idempotent et réservation conservatrice.

Les renommages sont atomiques sur un même système de fichiers ; aucune garantie
contre une panne électrique sans fsync n'est revendiquée. Les répertoires de staging
abandonnés après kill peuvent être nettoyés manuellement hors exécution.
Tous les workers doivent partager le même volume d'artefacts. Ne pas modifier un
prototype pendant sa génération. Aucun code produit par IA n'a été exécuté.
Cette revue ciblée ne constitue pas un audit de sécurité exhaustif.

## Résultats de cette revue

- Python 3.12 + PostgreSQL 16 réel : **57 réussis, 10 ignorés** (variantes SQLite
  de tests réservés à PostgreSQL). L'arrêt/redémarrage PostgreSQL est exécuté.
- Python 3.11 dans Docker, avec les sources courantes et PostgreSQL 16 réel :
  **56 réussis, 11 ignorés**. Le test d'arrêt du serveur n'est pas lancé depuis ce
  conteneur sans socket Docker ; il est couvert par le passage Python 3.12.
- Dépendances, compilation et vérification du diff : réussies.
- Ces validations locales ne prouvent pas que la CI GitHub distante est verte.

## Correction supplémentaire après synchronisation

Un montant juste inférieur à un milliard pouvait être arrondi à un milliard,
valeur hors capacité de NUMERIC(18,9). La borne est désormais vérifiée aussi
après arrondi, avant tout stockage. Les tests couvrent cette retenue et le plus
grand montant représentable.

Après synchronisation avec main le 8 novembre 2026 : **59 tests réussis,
10 ignorés** sous Python 3.12 avec PostgreSQL réel (y compris arrêt/redémarrage).
Compilation, contrôle des dépendances et diff validés. Les références Git des PR
confirment #1 et #2 ; aucune référence de PR complémentaire n'est encore présente.
