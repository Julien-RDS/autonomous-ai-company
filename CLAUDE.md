# Consignes pour Claude Code — Autonomous AI Company

Lis d'abord `README.md`, `docs/AVANCEMENT.md`, `docs/CLAUDE_HANDOFF.md` et les tests. Le code actuel est un MVP; ne présente jamais comme opérationnelles les fonctionnalités absentes.

## Contraintes impératives

- Conserver `MOCK_MODE=true` comme valeur par défaut; aucun appel payant pendant les tests.
- Ne jamais mettre des clés, `.env`, fichiers SQL réels, rapports privés ou données clients dans Git.
- Aucun paiement, emailing/prospection de masse, contrat, publication ni déploiement autonome sans autorisation humaine spécifique.
- Toute commande générée par IA doit rester non exécutable, sauf sandbox éphémère dédiée explicitement approuvée.
- Gestion des erreurs, limites de tokens, des coûts et des timeouts; borne stricte du nombre de tours.
- Tests pytest pour chaque modification; ne pas dégrader les commandes CLI existantes.
- Préparer les changements sur une branche `feat/...`, fournir PR avec description, tests et risques; ne pas pousser directement en production.

## Mission

Exécuter le plan dans `docs/CLAUDE_HANDOFF.md` étape par étape, en commençant par les correctifs prioritaires et la reproductibilité. Consigner les décisions d'architecture dans `docs/`.
