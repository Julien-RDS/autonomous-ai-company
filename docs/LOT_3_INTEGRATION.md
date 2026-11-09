# Intégration du bundle de validation du Lot 3

Le bundle fourni est valide et dépend du commit 706731e. Son commit bffdc83 est
conservé sans réécriture. Le patch joint produit le même patch-id que ce commit :
les deux fichiers représentent le même changement et ne sont appliqués qu'une fois.
La branche feat/lot-3-sandbox est synchronisée avec main (763b5d3, PR #4 fusionnée).
Aucune fusion de ces nouveaux changements dans main n'a été effectuée ici.

## Vérifications locales du 9 novembre 2026

- Python 3.12 avec PostgreSQL 16 réel : 134 réussis, 28 ignorés.
- Python 3.11 dans Docker avec PostgreSQL réel : 133 réussis, 29 ignorés.
- Dépendances, compilation Python et diff : vérifiés.
- Migration 008 et conservation des données : tests sur SQLite et PostgreSQL.
- Doctor réel : Docker joignable, rootless, cgroup v2 sans contrôleurs disponibles
  MemoryLimit, SwapLimit, PidsLimit et CpuCfsQuota. L'exécution doit rester refusée.

Les tests ignorés comprennent les variantes SQLite des scénarios PostgreSQL et
les intégrations d'exécution Docker, non exécutées sur ce démon sans contrôleurs.
Le passage 3.11 ignore aussi l'arrêt/redémarrage du serveur, exécuté en 3.12.
Les 150 réussites rapportées par Claude concernent son hôte rootful ; elles ne
sont pas présentées comme reproduites dans cet environnement.

Deux tests unitaires du nettoyage ciblé dépendaient involontairement de l'accès
réel à Docker malgré leurs conteneurs simulés. Quatre cas échouaient dans le
conteneur Python 3.11 sans client Docker. Le diagnostic du démon est désormais
simulé explicitement dans ces deux tests, sans modifier leurs assertions. Les
deux suites complètes ont ensuite été relancées avec succès.

## Publication

Les nouveaux changements sont destinés à une PR de brouillon contre main.
L'accès API GitHub de cet environnement était bloqué ; un push Git ne prouve
ni création de PR ni succès de la CI distante. Aucun appel payant, worker réel
ou prototype IA n'a été exécuté pendant cette intégration.
