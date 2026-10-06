# receptionniste-ia

Règles permanentes pour Claude dans ce dépôt.

## Données sensibles

- Ne jamais lire, afficher, modifier ni commiter : `.env`, `google-credentials.json`, les bases `*.db` (dont `salons.db`).
- Avant toute migration ou modification de données dans `salons.db`, faire une sauvegarde avec `./sauvegarde_db.sh` (crée un fichier daté dans `backups/`, jamais commité).
- Ne jamais modifier les données réelles de Belle Étoile pendant des tests : utiliser un salon de test séparé ou une copie de la base.

## Git

- Ne jamais lancer `git push` — l'utilisateur le fait lui-même.
- Ne commiter que du code testé. Message de commit en français.

## Produit

- La réceptionniste s'appelle Rachel (jamais un autre prénom), dans le code, l'UI et les prompts.

## Sécurité

- Sécurité d'abord : isolation entre comptes (toujours vérifier l'appartenance d'une ressource au compte courant), pas d'IDOR, validation des entrées, cookies httpOnly. Ne jamais affaiblir ces protections, même temporairement pour un test.

## Communication

- Après chaque changement, résumer ce qui a été fait et comment l'utilisateur peut le vérifier lui-même.
