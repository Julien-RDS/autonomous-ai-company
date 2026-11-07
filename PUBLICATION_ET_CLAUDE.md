# Publication sur GitHub et démarrage de Claude Code

## 1. Ubuntu / WSL — installer GitHub CLI

```bash
sudo apt update
sudo apt install -y gh
```

Authentifie-toi **avec Julien-RDS**, pas Julien2714 :

```bash
gh auth login
gh api user --jq .login
```

La deuxième commande doit afficher `Julien-RDS`.

## 2. Créer et publier le dépôt privé

Depuis le dossier de ce projet, qui contient `.git`, exécute :

```bash
./publish-github.sh
```

Cela crée le dépôt privé `Julien-RDS/autonomous-ai-company` et pousse la branche `main`. Si le dépôt privé existe déjà, le script publie la branche `main` sans recréer le dépôt. Il s'arrête si le compte actif n'est pas `Julien-RDS` ou si un dépôt du même nom est visible en public.

**Attention :** le fichier ZIP ne conserve généralement pas le dossier caché `.git`. Si ton extraction ne contient pas `.git`, restaure d'abord l'historique depuis le fichier `autonomous-ai-company-git.bundle` fourni séparément :

```bash
# Depuis le répertoire parent contenant le bundle :
git clone autonomous-ai-company-git.bundle autonomous-ai-company
cd autonomous-ai-company
```

Pour récupérer les fichiers additionnels fournis dans le ZIP, copie-les dans ce clone, puis `git add`, `git commit` et `./publish-github.sh`. Le plus simple reste d'utiliser le ZIP publication-ready fourni avec le dossier Git, si ton extracteur conserve `.git`.

Alternative si tu préfères le navigateur : crée un dépôt **privé et vide**, sans README ni .gitignore, sur https://github.com/new, puis :

```bash
git remote add origin https://github.com/Julien-RDS/autonomous-ai-company.git
git push -u origin main
```

## 3. Démarrer Claude Code

Installe Claude Code en suivant la documentation officielle : https://code.claude.com/docs/en/setup

Depuis la racine du dépôt :

```bash
claude
```

Puis colle le message :

> Lis `CLAUDE.md`, `docs/CLAUDE_HANDOFF.md`, `docs/AVANCEMENT.md` et `README.md`. Inspecte tout le code, puis commence le Lot 1 du handoff. Travaille sur une branche `feat/lot-1-audit`, vérifie les tests, corrige les problèmes avec tests de régression, et prépare une PR de brouillon. Ne lance aucun appel API payant ni déploiement sans validation. Donne-moi un bilan et le lien de PR à la fin.

Claude Code lit les instructions de `CLAUDE.md` dans le projet. Il doit être authentifié avec un compte Anthropic éligible ou une configuration API valide. N'accorde pas d'autorisations illimitées.
