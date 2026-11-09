# Correctif ciblé CI / cgroups

Le bundle b2d00a6 part de main 791ab06. Il est intégré sur
fix/ci-cgroup-diagnostics, sans appliquer 8f86c47 et sans nouvelle migration.
La sonde fixe prend en compte les chemins cgroup v1 et v2 (namespace privé ou
hôte) et affiche les valeurs observées. Le workflow publie ce diagnostic avant
les tests, et un résumé des tests échoués avec traceback court.

Le garde-fou antérieur de propriété de la sonde a été conservé : si la création
échoue, aucun conteneur préexistant ne doit être supprimé. Un test couvre ce cas.
La branche est ajoutée aux triggers push afin de vérifier sa CI même lorsque
l'API GitHub empêche la création automatique d'une PR.

Validation locale : 156 tests réussis, 28 ignorés sous Python 3.12 avec PostgreSQL
réel. Les intégrations d'isolation Docker restent non validées ici, faute de
contrôleurs ; elles ne sont pas remplacées par des assertions triviales.
La commande probe réelle signale uid 65534, mémoire 32 Gio, PID illimités, swap
illimité et quota 4 CPU, différents des limites demandées. Elle refuse l'hôte.
La lecture publique de GitHub Actions est possible ; la lecture API et les logs
restent bloqués. Une réussite locale ne prouve pas une CI verte. Ne pas fusionner
ce correctif avant confirmation du résultat de sa CI et de la PR.
