# Audit global — 10 novembre 2026

## Périmètre et niveau de livraison

Base vérifiée : `origin/main` à `c358f1b48e176d78f88d9259d566bfd0be05415e`
(PR #7). Sa [CI distante](https://github.com/Julien-RDS/autonomous-ai-company/actions/runs/42787501350)
est verte, vérifiée sur GitHub pendant cette revue.
Corrections et relais préparés sur `feat/project-readiness-audit` ; aucune fusion
automatique de cette branche.

Revue du workflow, adaptateurs, budget, configuration, persistance, migrations,
approbations, file/worker, génération, sandbox, supervision, Compose, CI et guides.
La suite existante couvre notamment concurrence, reprise et crashs PostgreSQL.
Cette revue ne constitue pas une certification exhaustive de sécurité.

## Défauts reproduits et corrigés

| Déclencheur | Avant | Après |
|---|---|---|
| Autorisation ancienne puis nouvelle image approuvée | La première autorisation bloquait la nouvelle | Sélection sur contenu, image configurée, profil et validateur ; ancienne autorisation non consommée |
| Résultat reçu après interruption avec données déjà enregistrées | `output_bytes`, troncature et résumé pouvaient être écrasés | Volume observé jamais réduit, troncature conservée, résumé existant préservé ; résultat tardif exact dans la preuve |
| Champs JSON de réunion de mauvais type ou résumé sandbox JSON invalide | Erreur serveur à la lecture | Champs de listes vérifiés et bornés ; résumé invalide absent dans l'API |
| Copie de la commande d'évaluation | `--success 0|1` était interprété comme un pipe shell | Exemple `--success 0` avec choix 0/1 expliqué |
| Prototype HTML de la démo | Suggestion du profil Python sans tests Python | Suggestion explicite `static-html`, image locale à configurer |

Les tests de régression ont échoué avant les corrections. Aucun schéma ni
historique n'est supprimé ; aucune migration supplémentaire ; `8f86c47` non appliqué.
La CI se déclenche maintenant sur toutes les branches `feat/**` et `fix/**`.
L'avancement et le relais Claude remplacent les instructions périmées de reprise du Lot 1.

## Vérification

Les commandes ci-dessous s'exécutent en MOCK. TEST_DATABASE_URL désigne une base
PostgreSQL jetable ; TEST_PG_CONTAINER uniquement son conteneur jetable, que le
test de panne arrête puis redémarre. Les secrets ne sont pas imprimés.

```bash
python -m pip check
python -m compileall -q company tests
MOCK_MODE=true python -m pytest -q --tb=short
git diff --check
```

Résultats locaux : Python 3.12 avec PostgreSQL 16 et test arrêt/redémarrage :
**201 réussis, 28 ignorés**. Python 3.11 dans un conteneur de revue avec PostgreSQL
16 : **200 réussis, 29 ignorés** (le conteneur n'a pas d'accès Docker pour le test
d'arrêt PostgreSQL). Les tests ignorés couvrent les variantes SQLite des tests
réservés à PostgreSQL et les intégrations sandbox non activées sur cet hôte.
Vérifications de dépendances, compilation et diff réussies. Un test ignoré ne
valide jamais sa fonctionnalité ; ces résultats locaux ne sont pas la CI distante.

Parcours réel Chromium : 30 pages/endpoints/téléchargements sur un serveur Uvicorn
local avec base temporaire, réunion MOCK, approbation et génération via les services
réels de l'application, zéro coût API. Rendu bureau 1440 px et mobile sombre 390 px,
pas d'erreur JavaScript ni de débordement horizontal du document. Les tableaux
larges restent défilables dans leur section. Les contenus générés sont lus comme
texte ; aucune exécution de prototype dans le navigateur ou sur l'hôte.

## Limites observées et décision d'utilisation

Le Docker cloud est rootless en cgroup v2 sans contrôleurs mémoire/swap/PIDs/CPU.
La sonde fixe observe mémoire 32 Gio, swap/PIDs sans plafond et quota CPU de 4 CPU,
au lieu des limites du sandbox. Le refus d'exécution est donc correct. Il faut un
hôte Docker capable pour utiliser réellement les tests isolés ; leur non-exécution
locale ne prouve pas leur succès. La CI GitHub les active avec une image épinglée.

**Utilisable aujourd'hui** : laboratoire personnel en simulation, consultations web,
interventions CLI, génération approuvée, reprise durable MOCK avec PostgreSQL ;
sandbox sur hôte compatible. Les connecteurs payants existent mais n'ont pas fait
l'objet d'appels réels pendant cet audit. Les réservations ne garantissent pas le
montant facturé par un fournisseur.

**Manque pour un pilotage entièrement web** : authentification, sessions, permissions,
CSRF, audit avec identité authentifiée et orchestration durable des actions, dont
le lancement sandbox. Le serveur web actuel reste local et en lecture seule.

**Manque pour une exploitation autonome réelle** : autorisation/reprise des appels
payants en worker, installation et restauration éprouvées sur l'hôte cible,
surveillance opérationnelle, puis éventuels connecteurs externes et validation
commerciale indépendante. Le scheduler actuel suppose une seule instance.
Les dépendances restent des plages compatibles, sans verrou complet.

## Suite bornée confiée à Claude

Voir [PROMPT_CLAUDE_FINALISATION.md](PROMPT_CLAUDE_FINALISATION.md) : console de
pilotage sécurisée, actions idempotentes et reprise sans réutiliser les approbations.
Après ses critères d'acceptation, le MVP sera pilotable par un opérateur dans son
navigateur. Cela ne termine pas une entreprise commerciale autonome et ne justifie
pas une exposition publique ou des dépenses sans surveillance.
