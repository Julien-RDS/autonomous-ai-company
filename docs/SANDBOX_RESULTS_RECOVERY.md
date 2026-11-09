# Résultats structurés et récupération confirmée

Cette évolution part de main b833e80 (PR #5 fusionnée). Elle conserve MOCK_MODE,
les approbations d'exécution à usage unique et le refus des hôtes sans limites.

## Nettoyage et concurrence

Défauts corrigés : suppression de workspace malgré échec de docker rm, absence
prise pour une panne de démon, course avec la préparation d'une exécution,
absence de trace si le parent meurt après suppression Docker avant commit SQL.

Un verrou noyau local par run est commun à l'exécuteur et aux nettoyages. Il
couvre toute l'exécution et est libéré automatiquement si le parent meurt.
Un parent vivant doit finir ou être arrêté par l'opérateur avant nettoyage ;
`cleanup --all` ne contourne pas ce verrou. Un run RUNNING sans conteneur avant
son échéance reste protégé : il peut être en préparation.

Le nettoyage persiste une intention `started` avant toute suppression. Après
Docker, une nouvelle liste doit confirmer l'absence du conteneur ET de sa sonde
avant d'effacer le workspace. Une panne ou une suppression refusée conserve les
fichiers et l'état SQL, avec `cleanup_failed`. Un crash laisse l'intention
`started` consultable ; répéter le nettoyage ciblé après échéance crée une
nouvelle trace sans prétendre que la première action avait été confirmée.
Les runs terminés sans ressources restantes ne produisent pas de nettoyages
répétitifs. Le nettoyage ne consomme pas d'approbation et ne relance aucun test.

Ces verrous supposent un seul hôte de supervision par démon Docker, un même
SANDBOX_WORKDIR et le même utilisateur système. Ils ne protègent pas contre des
opérations Docker manuelles d'un administrateur ni contre plusieurs clients sur
des hôtes indépendants. Les fichiers de verrou vides sont conservés hors des
workspaces pour éviter les courses d'inode ; ne pas les supprimer en activité.

## Rapports et provenance

La migration additive 009 ajoute : version du validateur aux autorisations et
aux runs, résumé JSON nullable et table sandbox_evidence (un résultat par run).
Les enregistrements historiques restent lisibles. Une autorisation ancienne
sans version ou liée à un validateur modifié exige une nouvelle revue explicite.
Une nouvelle autorisation valide n'est pas bloquée par un ancien enregistrement.

Le hash du validateur porte sur la commande fixe et son script. L'image exacte
et le digest du manifeste restent conservés. Le runner unittest produit un
résumé borné : discovered, run, passed, failed, errors, skipped,
expected_failures, unexpected_successes. Le profil HTML produit un constat de
structure, jamais un résultat fonctionnel JavaScript.

Le parseur refuse les compteurs non entiers, valeurs excessives, schémas
inconnus, comptes incohérents, doublons ou sorties tronquées. Un résumé absent
ou invalide reste indisponible, jamais remplacé par des compteurs inventés.
Des subtests produisant plusieurs échecs par test peuvent rendre le décompte
non représentable ; dans ce cas les sorties brutes restent la preuve disponible.

**Tout rapport du conteneur est une déclaration non fiable** : du code testé peut
falsifier ses sorties. Il ne transforme jamais un échec de l'exécuteur en succès et n'affecte ni les
approbations, ni le budget, le routage ou une évaluation commerciale. Un code de
sortie zéro sans rapport valide est classé ERROR, sans compteurs inventés.
Le dashboard et GET /api/sandbox-runs présentent le résumé comme déclaré, avec
version, image et manifeste. Les sorties brutes ne sont pas exposées par cet API.

La réponse de l'exécuteur est conservée dans sandbox_evidence, bornée et liée au
run, même après interruption. Elle ne remplace pas un verdict INTERRUPTED ni
une sortie antérieure déjà enregistrée. Le verdict final et le nettoyage
verrouillent la ligne SQL ; les validations SQLite sont sérialisées également.

## Vérification et lancement

```bash
python -m pip install -r requirements.txt
alembic upgrade head
MOCK_MODE=true python -m pytest -q
# Définir TEST_DATABASE_URL vers PostgreSQL jetable pour les intégrations SQL.
# TEST_SANDBOX_IMAGE nécessite un Docker avec ressources réellement limitées.
python -m company sandbox doctor
python -m company dashboard
```

Tests ajoutés : suppression refusée, démon perdu, interruption après suppression,
intention conservée puis reprise, verrou entre processus et libération après
mort du parent, ancien validateur, preuves tardives et rapports falsifiés.
Le runner est vérifié sur des fixtures écrites par les développeurs. Aucun
prototype IA ni appel payant n'a été exécuté pendant ces travaux.
Le Docker cloud reste rootless sans contrôleurs : les tests d'isolation y sont
non validés. Le workflow CI inclut cette branche et doit être observé sur un
runner compatible ; le refus n'est jamais désactivé pour obtenir du vert.

## Résultats locaux du 9 novembre 2026

- Python 3.12, PostgreSQL réel : **150 réussis, 28 ignorés**.
- Python 3.11 dans Docker, PostgreSQL réel : **149 réussis, 29 ignorés**.
- Le passage 3.12 inclut l'arrêt/redémarrage PostgreSQL ; pas le passage conteneur 3.11.
- Les intégrations d'isolation Docker sont non validées dans ces passages :
  TEST_SANDBOX_IMAGE non défini sur ce démon rootless dépourvu de contrôleurs.
  Les variantes SQLite des scénarios exclusivement PostgreSQL sont ignorées aussi.
- Dépendances, compilation Python, YAML CI et diff vérifiés. Doctor confirme
  le refus de ce démon. Aucun résultat distant GitHub Actions n'est affirmé.
