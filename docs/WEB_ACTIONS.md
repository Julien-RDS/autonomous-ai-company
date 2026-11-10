# Actions web locales (opt-in)

Le tableau de bord reste **en lecture seule par défaut**. Les routes GET publiques au processus local
ne donnent aucune permission d'écriture. N'exposez **jamais** ce serveur directement sur Internet,
sur un LAN non fiable ou derrière un proxy sans protection additionnelle.

## Activation

Le serveur reste lié à `127.0.0.1:8000`. Dans un terminal local :

```bash
export AAC_WEB_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export AAC_WEB_SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export AAC_WEB_PERMISSIONS="approve,requeue,reconcile"
python -m company dashboard
```

Conservez les deux secrets dans un gestionnaire de secrets ou une configuration locale protégée.
Le jeton administrateur doit être copié depuis l'environnement où il a été généré ; ne l'inscrivez
ni dans Git ni dans les journaux. Ouvrez `http://127.0.0.1:8000/actions` et connectez-vous.

Les deux secrets doivent être **distincts** et comporter au moins 32 caractères chacun.
Une mauvaise configuration empêche le démarrage (échec fermé).
La session HttpOnly SameSite=Strict expire après une heure ; elle est signée par HMAC.
La modification de l'un des deux secrets invalide les sessions. L'authentification ne se substitue
pas à un contrôle d'accès réseau. Aucun compte utilisateur distant ni récupération de mot de passe n'est fourni.

## Permissions indépendantes

Les permissions possibles sont `approve`, `prototype`, `requeue`, `reconcile`,
`sandbox_approve` et `sandbox_run`. Seules celles explicitement listées dans
`AAC_WEB_PERMISSIONS` affichent des formulaires et autorisent leur endpoint POST.

- `approve` : valide une tâche, **sans** autoriser une exécution.
- `prototype` : génère les fichiers, réservé au mode MOCK ; **n'exécute** pas ces fichiers.
- `requeue` : remet en file un travail MOCK précédemment échoué et audite le motif.
- `reconcile` : rapproche un coût confirmé par un relevé, **ne relance** aucun appel API.
- `sandbox_approve` : approbation d'exécution après relecture explicite, image Docker
  autorisée par SHA-256 et profil prédéfini ; usage unique et contenu lié au manifeste.
- `sandbox_run` : opération **distincte**, nécessitant une autorisation encore valide
  et une confirmation visible avant le démarrage du test en conteneur. N'accordez cette
  permission que pendant une session de maintenance supervisée.

Exemple explicite pour un test sandbox : `AAC_WEB_PERMISSIONS=sandbox_approve,sandbox_run`.
La présence d'une autorisation de génération n'active jamais une autorisation d'exécution.
Le backend conserve toutes les contraintes et écritures d'audit existantes.

## Défenses et limites

- POST uniquement pour les mutations et contrôle d'origine/referer ; formulaires URL-encodés bornés.
- CSRF signé dans une session HttpOnly ; cookie SameSite=Strict ; absence de scripts côté client.
- Authentification par jeton fort en configuration locale, comparé en temps constant.
- Autorisations serveur **avant** toute opération ; aucune permission implicite.
- Les opérations réutilisent les transactions, validations d'état et protections d'idempotence du CLI.
- Réponse 303 après succès ; pas de répétition automatique après rafraîchissement.
- Politique de référent `same-origin` : les formulaires Chromium conservent une
  origine vérifiable ; les origines nulles ou étrangères restent refusées.
- En cas d'erreur de validation, une réponse neutre est renvoyée au navigateur.
- Cette implémentation n'a ni anti-bruteforce interprocessus ni gestion multi-comptes :
  usage **local uniquement**, pas une solution IAM de production.
- Les exécutions sandbox restent soumises aux limites Docker documentées dans
  `docs/LOT_3_SANDBOX.md`. Docker n'est pas une frontière absolue.
- **Pas d'appel API externe automatique depuis les actions web.**

Les tests couvrent le mode désactivé, l'authentification, l'origine, le CSRF,
l'altération de cookie, le cloisonnement des permissions et les effets en base.
Le parcours Chromium connexion → approbation → génération MOCK → déconnexion est
vérifié par l'audit global. Toute exposition autre que localhost reste hors périmètre.

## Limites de reprise à traiter

La génération et le sandbox tournent actuellement dans la requête HTTP, sans
registre persistant des demandes web. Deux envois du formulaire d'approbation
sandbox créent deux autorisations ; deux demandes de lancement peuvent consommer
deux autorisations disponibles. La redirection 303 ne garantit pas l'idempotence
entre requêtes concurrentes. Ne pas présenter ces actions comme un pilotage durable.

La déconnexion supprime le cookie dans le navigateur ; une copie du cookie reste
valide jusqu'à expiration ou rotation des secrets suivie du redémarrage du serveur.
La révocation individuelle et la durabilité sont dans
[la mission de finalisation](PROMPT_CLAUDE_FINALISATION.md).
