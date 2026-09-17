# Apartment Watcher — Structura + Living Stone

Surveille les **appartements à louer** publiés par Structura et Living Stone et envoie une notification Telegram quand une annonce correspond à :

- loyer **≤ 1 400 €/mois** ;
- **au moins 2 chambres** ;
- toutes les localités ;
- annonces encore disponibles (les statuts « en option », « loué avec succès » et « visites complètes » sont ignorés).

Le workflow GitHub Actions tourne toutes les **15 minutes**. Le premier lancement sert uniquement à créer une référence des annonces déjà présentes : il n'envoie pas une avalanche de notifications.

## 1. Créer le bot Telegram

1. Dans Telegram, ouvre **@BotFather**.
2. Envoie `/newbot` et suis les instructions.
3. Copie le **token** fourni par BotFather.
4. Ouvre une conversation avec ton nouveau bot et envoie-lui n'importe quel message (par exemple `hello`).
5. Pour obtenir ton `chat_id`, ouvre dans un navigateur l'endpoint Telegram `getUpdates` avec le token de ton bot, puis cherche `message.chat.id` dans la réponse JSON.

Exemple de commande si tu préfères le terminal :

```bash
curl "https://api.telegram.org/bot<TON_TOKEN>/getUpdates"
```

## 2. Créer le repo GitHub

Crée un repo GitHub et copie tout le contenu de ce dossier dedans, puis pousse-le sur la branche principale.

## 3. Ajouter les secrets GitHub

Dans le repo : **Settings → Secrets and variables → Actions → New repository secret**.

Ajoute :

- `TELEGRAM_BOT_TOKEN` : le token donné par BotFather ;
- `TELEGRAM_CHAT_ID` : la valeur numérique du `chat.id` Telegram.

Ne mets jamais le token directement dans le code.

## 4. Premier lancement

Va dans **Actions → Apartment watcher → Run workflow**.

Le premier run remplit `seen.json` avec les annonces actuelles sans envoyer de notification. À partir du run suivant, toute nouvelle annonce qui correspond aux critères déclenche Telegram.

## Modifier les critères

Édite `config.json` :

```json
{
  "max_rent": 1400,
  "min_bedrooms": 2,
  "check_all_locations": true,
  "sources": {
    "structura": true,
    "living_stone": true
  }
}
```

## Modifier la fréquence

Dans `.github/workflows/watch.yml`, la ligne :

```yaml
- cron: "*/15 * * * *"
```

correspond à un contrôle toutes les 15 minutes.

## Fonctionnement

Le watcher utilise Chromium via Playwright afin de fonctionner aussi avec les pages dont les annonces sont rendues en JavaScript. Il découvre les URLs des fiches, ouvre chaque fiche, extrait le loyer, le nombre de chambres et la surface, puis utilise :

- l'identifiant numérique de l'URL pour Structura ;
- la référence `#xxxxx` quand elle existe chez Living Stone ;

comme identifiant stable de l'annonce.

`seen.json` est automatiquement mis à jour et commité par GitHub Actions. Une annonce déjà notifiée n'est donc pas renvoyée à chaque exécution.

## Tester localement

```bash
pip install -r requirements.txt
python -m playwright install chromium
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python watcher.py
```

Pour les petits tests du parseur :

```bash
pip install pytest
pytest -q
```

## Note pratique

Les sites immobiliers peuvent modifier leur HTML. Le script évite volontairement des sélecteurs CSS très spécifiques et extrait surtout les informations depuis le texte rendu de chaque fiche, ce qui le rend plus tolérant aux changements de mise en page. Si un site change profondément sa structure, le workflow échouera plutôt que de considérer à tort qu'il n'y a aucune annonce.
