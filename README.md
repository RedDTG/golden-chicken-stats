# Golden Chicken Stats

Bot Discord qui surveille les résultats de cockfight d'UnbelievaBoat et les journalise dans une Google Sheet, avec un onglet récapitulatif par joueur tenu à jour automatiquement.

## Fonctionnalités

- **Journalisation en temps réel** : chaque victoire/défaite de cockfight postée par UnbelievaBoat est parsée et ajoutée comme ligne dans la feuille `Cockfights` (joueur, résultat, gain, probabilité, horodatage, serveur).
- **`/backfill`** (réservée aux administrateurs) : rescanne tout l'historique du salon surveillé (et de ses fils, actifs et archivés) pour corriger les lignes déjà loguées et ajouter celles qui manquent. Utile après une correction de bug de parsing ou une coupure du bot.
- **`/stats`** : affiche un embed avec les statistiques du serveur (combats, victoires, défaites, meilleur/pire winrate, poulet obtenu à la plus haute probabilité).
- **Feuille `Overview`** : une ligne par joueur (+ une ligne `Total`) avec Total / Défaites / Victoires / Winrate / Meilleur poulet / Rentabilité / ID Joueur, recalculée automatiquement à chaque combat logué et après chaque backfill. Regroupée par ID Discord (pas par pseudo affiché) pour éviter de fusionner deux membres qui partagent le même pseudo/surnom — les lignes créées avant l'ajout de cette colonne sont corrigées au prochain `/backfill`.
- **Feuille `Bank`** : journalise en temps réel chaque `+bal` observé dans le salon surveillé (par n'importe qui, pas seulement les joueurs suivis) concernant un membre listé dans `TRACKED_MEMBER_IDS` (joueur, cash, banque, total, horodatage, serveur). Écoute passive uniquement — le bot n'envoie jamais `+bal` lui-même. Non couvert par `/backfill` (temps réel uniquement).

## Prérequis

- Python 3.12+
- Un bot Discord (Discord Developer Portal)
- Une Google Sheet et un compte de service Google avec accès à l'API Sheets

## Installation

```bash
pip install -r requirements.txt
cp .env.example .env   # puis renseigner les valeurs
python bot.py
```

## Configuration

Toutes les variables sont documentées dans [.env.example](.env.example) et chargées via `python-dotenv` :

| Variable | Obligatoire | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | oui | Token du bot (Developer Portal > Bot > Reset Token). |
| `WATCH_CHANNEL_ID` | non | Salon à surveiller. Vide = tous les salons visibles par le bot. |
| `UNBELIEVABOAT_ID` | non | Si défini, seuls les embeds de ce bot sont pris en compte. |
| `GOOGLE_CREDENTIALS_FILE` | non (défaut `credentials.json`) | Fichier de clé du compte de service Google. |
| `GOOGLE_CREDENTIALS_JSON` | non | Contenu JSON de la clé directement en variable d'environnement, prioritaire sur `GOOGLE_CREDENTIALS_FILE` (pratique en déploiement, ex. Coolify). |
| `GOOGLE_SHEET_ID` | oui | ID de la feuille (dans son URL). |
| `GOOGLE_SHEET_TAB` | non (défaut `Cockfights`) | Onglet des lignes de combats, doit déjà exister. |
| `OVERVIEW_SHEET_TAB` | non (défaut `Overview`) | Onglet récapitulatif par joueur, doit déjà exister. |
| `TIMEZONE` | non (défaut `Europe/Paris`) | Fuseau IANA utilisé pour l'Horodatage, indépendant du fuseau système de la machine qui héberge le bot. |
| `BANK_SHEET_TAB` | non (défaut `Bank`) | Onglet des balances suivies, doit déjà exister. |
| `TRACKED_MEMBER_IDS` | non | IDs Discord séparés par des virgules des membres dont les `+bal` doivent être journalisés dans `Bank`. Vide = suivi désactivé. |

### Créer le bot Discord

1. [Discord Developer Portal](https://discord.com/developers/applications) > New Application.
2. Onglet **Bot** > Reset Token, copier la valeur dans `DISCORD_BOT_TOKEN`.
3. Toujours dans **Bot**, activer le **Message Content Intent** (Privileged Gateway Intents) — nécessaire pour lire le contenu des messages/embeds.
4. Onglet **OAuth2 > URL Generator** : cocher les scopes `bot` **et** `applications.commands` (indispensable pour que `/backfill` et `/stats` s'enregistrent), puis les permissions `View Channels`, `Send Messages`, `Read Message History`. Utiliser l'URL générée pour inviter le bot sur le serveur.

### Créer le compte de service Google

1. [Google Cloud Console](https://console.cloud.google.com/) > créer un projet (ou en réutiliser un).
2. Activer l'**API Google Sheets** pour ce projet.
3. **IAM et administration > Comptes de service** > créer un compte de service.
4. Sur ce compte de service, onglet **Clés** > Ajouter une clé > JSON : télécharger le fichier et le placer à l'emplacement pointé par `GOOGLE_CREDENTIALS_FILE` (ou copier son contenu dans `GOOGLE_CREDENTIALS_JSON`).
5. Partager la Google Sheet cible avec l'adresse e-mail du compte de service (rôle **Éditeur**), sinon le bot ne pourra ni lire ni écrire dedans.
6. Créer dans la feuille les deux onglets `Cockfights` et `Overview` (les en-têtes sont créés/corrigés automatiquement par le bot au démarrage).

## Backfill manuel

En plus de `/backfill`, le rescan est aussi disponible en ligne de commande :

```bash
python backfill.py
```

Réutilise la configuration et la logique de `bot.py` (`run_backfill()`). Nécessite la permission "Read Message History" sur le(s) salon(s) surveillé(s).

## Déploiement

Un `Dockerfile` est fourni (image `python:3.12-slim`). En déploiement (ex. Coolify), utiliser `GOOGLE_CREDENTIALS_JSON` plutôt que de fournir un fichier de credentials.

## Notes

- Il n'y a pas de suite de tests, linter, ou étape de build dans ce dépôt.
- Voir [CLAUDE.md](CLAUDE.md) pour le détail de l'architecture (parsing des embeds, logique de backfill, calcul des stats).
