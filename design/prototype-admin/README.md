# Prototype cliquable de l'administration (ticket HULL-18)

- Date : 2026-10-28
- URL : https://claude.ai/artifact/4M3vN18F2YRD82oZQZoSSq
- Statut : validé par l'opérateur (« on est bien pour cette base »)
- Référence de design pour HULL-11, avec `design/amaterasu/`.

## Fichiers

- `project/Main.dc.html` : page principale (source du composant de design, copie de référence ; le moteur `support.js` n'est pas copié, la page se consulte via l'URL).
- `project/canvas.json` : index de la maquette.

## Ce qui est simulé

Tout. Aucune donnée réelle, aucun appel réseau, aucun serveur : arbre, bibliothèque de connexions, placements, aperçu des droits, confirmation de connexion partagée et journal d'audit sont des données d'exemple en mémoire de la page. Les jetons et identifiants affichés sont fictifs.
