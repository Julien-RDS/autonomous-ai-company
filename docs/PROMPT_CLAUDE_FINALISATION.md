# Mission Claude — finaliser le pilotage web du MVP

Tu es responsable de la dernière étape du MVP supervisé. Implémente la mission
jusqu'à ses tests et sa livraison ; ne t'arrête pas à un plan.

**Dépôt : `Julien-RDS/autonomous-ai-company`**
https://github.com/Julien-RDS/autonomous-ai-company

## Contexte et base de travail

L'application Python orchestre un conseil OpenAI, Anthropic et Gemini : proposer
des opportunités, rechercher éventuellement, critiquer, voter, décider et créer
des tickets. Elle conserve l'historique et les coûts. Un humain autorise la
génération d'un prototype, puis autorise séparément son exécution en sandbox.
Elle n'a aucune preuve autonome de rentabilité.

Sont implémentés : SQLAlchemy/Alembic, SQLite et PostgreSQL, montants Decimal,
file durable, leases et checkpoints LangGraph pour le MOCK, génération durable,
sandbox Docker contraint, autorisations à usage unique, preuves et nettoyages
audités, supervision FastAPI en lecture seule.

PR #7 fusionnée dans main (`c358f1b`). La branche `feat/project-readiness-audit`
corrige les autorisations après changement d'image, les résultats tardifs, les
données malformées à l'affichage et les commandes CLI proposées ; elle actualise
la documentation et les déclencheurs CI.
Lis CLAUDE.md, docs/AVANCEMENT.md, docs/AUDIT_GLOBAL_2026_11.md et les tests.
Fetch les références et confirme SHA/CI. Si l'audit n'est pas dans main, pars de
sa branche et explicite la dépendance dans la PR ; sinon pars de main.
Branche proposée : `feat/secure-web-control`.

Ne réapplique pas `8f86c47` : conflit avec `009_result_evidence`. Inspecte la tête
Alembic avant toute migration additive.

## Objectif concret

Un opérateur doit pouvoir se connecter, lancer une réunion MOCK, examiner les
résultats, approuver puis demander une génération, lire les fichiers, accorder
une autorisation distincte puis demander un test isolé, consulter son résultat et
consigner son évaluation, sans copier des commandes après l'installation.
Un double clic, un rechargement, deux requêtes concurrentes ou un redémarrage ne
doivent ni doubler une mutation ni réutiliser une autorisation consommée.

Le cœur complexe est la **liaison transactionnelle entre identité, demande,
approbation précise et travail durable**, notamment si le worker meurt au lancement
du sandbox. Commence par ce modèle d'états et ses invariants, puis implémente
l'interface qui les utilise.

## Périmètre à livrer

1. Authentification locale explicite, sans compte/mot de passe par défaut.
   Initialisation/récupération par CLI, hachage avec une bibliothèque éprouvée,
   sessions expirables et révocables, protection contre les essais répétés.
   Cookies HttpOnly/SameSite, Secure sous HTTPS ; documenter HTTP loopback.
   Protéger aussi API et téléchargements. Rester sur 127.0.0.1 par défaut.
   Contrôler Host/Origin et CSRF sur les mutations, connexion/déconnexion comprises.
   L'identité d'audit vient de la session, jamais d'un champ actor du formulaire.

2. Permissions serveur : consultation et intervention. Mutations par POST ; GET
   sans effet métier. Partager les services métier entre CLI et web, sans commande
   shell issue d'un formulaire. Confirmation explicite des effets, erreurs neutres,
   identifiants d'action et résultat consultable après redirection.

3. Actions : soumettre une réunion MOCK, approuver une génération et la mettre
   en file, approuver un sandbox après relecture et demander son lancement,
   remettre un travail admissible en file, consigner une évaluation, rapprocher
   un coût inconnu avec référence, demander un nettoyage ciblé admissible.
   Conserver tous les refus métier ; ne pas exposer de nettoyage global forcé.

4. Idempotence persistante : clé liée à l'acteur, l'action, la cible et au contenu
   normalisé. Même clé/même demande retourne le résultat initial ; contenu
   différent avec la même clé refusé. Unicité en base entre processus.
   Mutation et audit atomiques, sans secrets.

5. Aucun traitement long dans HTTP ni simple BackgroundTasks servant de file.
   Réutiliser la file existante pour réunions/génération. Ajouter le chemin durable
   sandbox avec une approbation **identifiée explicitement**, liée au digest,
   profil, image, version du validateur et durée de validité. Prévoir expiration/
   révocation des nouvelles autorisations, documenter le traitement des anciennes.
   Recontrôler les contraintes au lancement. Un worker ayant perdu son lease ne
   peut plus finaliser l'opération.

6. Définir/tester les crashs avant/après consommation de l'autorisation, création
   du conteneur et persistance du résultat. Une exécution ambiguë attend diagnostic/
   revue humaine ; ne jamais consommer une autre approbation pour la rejouer.
   Conserver preuves et résultats tardifs. Le worker sandbox dédié détient l'accès
   Docker ; le serveur web n'a pas besoin du socket. Documenter le stockage partagé.

## Contraintes et définition de fini

- MOCK par défaut, workers payants désactivés, aucun appel réel pendant les tests.
  Les accès réels CLI ne doivent pas être implicitement exposés sur le web.
- Aucun code généré sur l'hôte, pas de commande libre ni d'image fournie par un
  formulaire. Conserver sonde des limites effectives et refus sandbox.
- Préserver XSS/CSP, rendu texte des prototypes, montants exacts et séparation
  des approbations. GitHub, emails, paiements, navigateur agentique et publication
  publique sont hors de ce lot.
- PostgreSQL est la référence du pilotage durable. Conserver lecture/CLI SQLite ;
  refuser clairement les actions nécessitant PostgreSQL lorsque celui-ci manque.
- Tests SQLite/PostgreSQL et Python 3.11/3.12 : accès anonyme, session expirée,
  révocation, CSRF/Origin, mauvais rôle, requêtes concurrentes, contenu/image/
  validateur modifiés, autorisation expirée/consommée, crashs et reprise.
- Vrai parcours navigateur du scénario MOCK complet, avec double soumission et
  reconnexion après redémarrage. Docker réel sur hôte capable ; tests ignorés
  explicitement documentés sur l'hôte cloud sans contrôleurs de ressources.
- Procédure d'installation : initialisation du compte, migrations, démarrage
  web/worker, sauvegarde et restauration de la base ET des artefacts. Vérifier une
  restauration de données de test dans une nouvelle base.
- Livrer commits, PR de revue, commandes, résultats exacts, captures, limites et
  CI distante verte pour le SHA livré. Ne pas fusionner ni déployer dans cette mission.

Le résultat attendu est un **MVP personnel supervisé depuis le navigateur**, avec
reprise durable en simulation. L'autonomie commerciale et l'exploitation payante
sans surveillance ne font pas partie de cette définition de fini. Si un critère
échoue, le nommer et le corriger au lieu d'annoncer le projet terminé.
