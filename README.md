# 🛟 Hulpdashboard

Een Discord-bot die een niet-technische gebruiker (in dit geval: pa) helpt met alledaagse vragen over
computer, tv, streamingdiensten en wifi, én met Twitch, OBS en StreamElements. Hij stelt zijn vraag in
`#hulp` (mag met een screenshot of foto), en de bot antwoordt in eenvoudig Nederlands, in maximaal
3-4 stappen.

Alles beheer je via een webdashboard: tokens, model, kanalen, de systeemprompt, het geheugen en de
gesprekken. Na `docker compose up -d` hoef je op de server nooit meer bestanden aan te maken of
configuratie aan te passen.

- **Bot:** Python 3.12 + discord.py
- **Dashboard:** FastAPI + eenvoudige HTML-pagina's (werkt ook op je telefoon, met licht/donker thema)
- **Opslag:** SQLite; geheimen versleuteld (Fernet)
- **AI:** OpenRouter met gratis vision-modellen en automatische fallback
- **Image:** gebouwd door GitHub Actions (amd64 + arm64), gepubliceerd op `ghcr.io`

---

## Inhoud

1. [Hoe het werkt](#hoe-het-werkt)
2. [Eerste installatie op je server](#eerste-installatie-op-je-server)
3. [De image van ghcr.io kunnen ophalen (privé repo)](#de-image-van-ghcrio-kunnen-ophalen-privé-repo)
4. [Discord-bot aanmaken](#discord-bot-aanmaken)
5. [OpenRouter-key aanmaken](#openrouter-key-aanmaken)
6. [De setup-wizard](#de-setup-wizard)
7. [Toegang tot het dashboard](#toegang-tot-het-dashboard)
8. [Updaten, vastzetten en terugrollen](#updaten-vastzetten-en-terugrollen)
9. [Automatisch updaten met Watchtower (optioneel)](#automatisch-updaten-met-watchtower-optioneel)
10. [Beveiliging: wat wel en niet beschermd is](#beveiliging-wat-wel-en-niet-beschermd-is)
11. [Back-up](#back-up)
12. [Gebruik in Discord](#gebruik-in-discord)
13. [Ontwikkelen en testen](#ontwikkelen-en-testen)
14. [Projectstructuur](#projectstructuur)

---

## Hoe het werkt

```
┌──────────────────────── container "hulpdashboard" (uid 10001) ─────────────────────────┐
│  uvicorn ─┬─ FastAPI-dashboard  :8080  ──(alleen 127.0.0.1 op de host)                   │
│           └─ Discord-bot (asyncio-taak in hetzelfde proces)                              │
│                                                                                          │
│  volume hulp-data → /data   SQLite (instellingen, versleutelde geheimen, gesprekken,     │
│                             geheugen, logs) + afbeeldingen                               │
│  volume hulp-keys → /keys   secret.key (Fernet-sleutel, chmod 600)                       │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

Het dashboard en de bot draaien bewust in **één container en één proces**. Daardoor kan het dashboard de
bot direct opnieuw laten verbinden als je het Discord-token vervangt, zonder herstart en zonder trucs
tussen containers. Alle andere instellingen (model, kanalen, prompt, geheugenmodus) leest de bot bij elk
bericht opnieuw uit de database, dus wijzigingen werken meteen.

**Een vraag van pa, stap voor stap:**

1. Hij typt in `#hulp` (of in een draadje daarin), eventueel met een afbeelding.
2. Wachtwoorden, pincodes, IBANs, kaartnummers en Twitch-streamsleutels worden uit de tekst gepoetst
   voordat er iets wordt opgeslagen of naar het model gaat. Foto's worden verkleind en EXIF-gegevens
   (zoals GPS-locatie) worden verwijderd.
3. Gaat het over **betalen, bankzaken, wachtwoorden, verdachte mails/sms'jes of abonnementen
   opzeggen**? Dan stuurt de bot de vraag níét naar het model, maar antwoordt: *"Klik voorlopig nergens op
   … @jij kun je hier even naar kijken?"*. Het model kan dit ook zelf signaleren (bijv. bij een
   screenshot van een verdachte mail).
4. Anders gaat de vraag, met de systeemprompt, zijn profiel, de onthouden feiten en de laatste ~10
   berichten, naar OpenRouter. Lukt het eerste model niet (fout, rate limit, leeg antwoord), dan
   probeert de bot automatisch het volgende model in de lijst. Tijdens het nadenken zie je "typing…".
5. Na een paar minuten stilte vraagt de bot het model (met een kort extra verzoek) of er een nieuw,
   blijvend feit in het gesprek zat. Nieuwe feiten verschijnen in `#geheugen`: direct opgeslagen, of
   eerst ter goedkeuring (✅/❌ eronder, of in het dashboard).

---

## Eerste installatie op je server

Je hebt alleen Docker met de Compose-plugin nodig. Je hoeft de repo **niet** te clonen.

```bash
mkdir -p ~/hulpdashboard && cd ~/hulpdashboard
```

Zet `docker-compose.yml` uit deze repo in die map (kopiëren met `scp`, of plakken met een editor).
Daarna:

```bash
docker compose up -d
```

Controleer of hij draait (na ±40 seconden zou de status `healthy` moeten zijn):

```bash
docker compose ps
```

Open nu het dashboard (zie [Toegang tot het dashboard](#toegang-tot-het-dashboard)) en doorloop de
setup-wizard. Er is geen `.env`-bestand en je hoeft verder niets op de server te configureren.

> **Let op:** wie het dashboard als eerste opent, mag het wachtwoord kiezen. Daarom luistert de poort
> standaard alleen op `127.0.0.1`. Doorloop de wizard meteen na de eerste start.

---

## De image van ghcr.io kunnen ophalen (privé repo)

Bij elke push naar `main` bouwt GitHub Actions de image en zet hem op
`ghcr.io/iitssjstn/hulpdashboard` (tags `latest` en de korte commit-SHA, bijv. `3f9c2ab`).
Daarvoor zijn geen extra secrets nodig: de workflow gebruikt de ingebouwde `GITHUB_TOKEN`.

Je repo mag privé blijven. Een package van een privé repo is standaard óók privé, dus je server moet
hem op een van deze twee manieren kunnen ophalen:

**Optie A: package public maken (het simpelst).** De image bevat géén geheimen: tokens, keys en het
wachtwoord staan alleen versleuteld in de database op jouw server. Wel kan iedereen dan de code in de
image bekijken.

1. Wacht tot de eerste workflow-run klaar is (tabblad **Actions** in je repo, groen vinkje).
2. Ga naar je GitHub-profiel → **Packages** → `hulpdashboard`
   (of `https://github.com/users/iitssjstn/packages/container/package/hulpdashboard`).
3. Klik rechts op **Package settings**.
4. Onderaan bij **Danger Zone** → **Change visibility** → **Public** → bevestig met de naam.

**Optie B: package privé houden en de server eenmalig laten inloggen.**

1. GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)** →
   **Generate new token (classic)**.
2. Vink alléén `read:packages` aan. Kies een vervaldatum (of geen) en maak het token aan.
3. Op de server:

   ```bash
   docker login ghcr.io -u iitssjstn
   ```

   Plak bij "Password" het token. Docker bewaart de login in `~/.docker/config.json`. Dit token kan
   alleen packages lezen, niets anders.

---

## Discord-bot aanmaken

1. Ga naar <https://discord.com/developers/applications> → **New Application** → geef een naam (bijv.
   "Hulpbot") en maak hem aan.
2. Tabblad **Bot**:
   - Zet onder **Privileged Gateway Intents** de **Message Content Intent** AAN (verplicht, anders kan
     de bot de vragen niet lezen) en klik **Save Changes**.
   - Klik **Reset Token** en kopieer het token. Dit plak je in de wizard, niet ergens anders.
   - Zet eventueel **Public Bot** uit, zodat alleen jij hem kunt uitnodigen.
3. **Uitnodigen:** klik in de wizard (of bij Instellingen) op **Test** naast het token. Je krijgt dan een
   link "Bot uitnodigen op je server" met precies de juiste rechten:
   kanalen bekijken, berichten sturen (ook in draadjes), berichtgeschiedenis lezen, links insluiten,
   reacties toevoegen en berichten beheren (om de ✅/❌-reacties in `#geheugen` op te ruimen).

   Handmatig kan ook: **OAuth2** → **URL Generator** → scope `bot` → de rechten hierboven.
4. Maak in je server de kanalen `#hulp` en `#geheugen` aan. Geef de bot toegang tot beide. Maak
   `#geheugen` eventueel privé voor jezelf en de bot.
5. Zet in Discord **Gebruikersinstellingen → Geavanceerd → Ontwikkelaarsmodus** aan, zodat je met
   rechts klikken **ID kopiëren** kunt doen op de server, de kanalen en jezelf.

---

## OpenRouter-key aanmaken

1. Maak een account op <https://openrouter.ai>.
2. **Keys** → **Create Key**. Een kredietlimiet van $0 is prima: de bot gebruikt gratis modellen.
3. Plak de key in de wizard en klik **Test**.

**Gratis modellen** hebben limieten: ongeveer 20 verzoeken per minuut en 50 per dag zonder tegoed (een
paar honderd tot duizend per dag als je eenmalig wat tegoed koopt). Elke vraag kost één verzoek, en het
geheugen kost ongeveer één extra verzoek per gesprek. Voor één persoon is dat ruim genoeg.

**Welk model?** Standaard: `google/gemma-4-31b-it:free` (leest afbeeldingen), met als fallbacks
`google/gemma-4-26b-a4b-it:free` en `openrouter/free` (OpenRouter kiest dan zelf een gratis model dat
afbeeldingen aankan). Het gratis aanbod verandert regelmatig. Klik daarom bij **Instellingen** op
**Actuele gratis vision-modellen ophalen**: je ziet dan live welke gratis modellen afbeeldingen kunnen
lezen, en met één klik zet je er een als hoofd- of fallbackmodel.

---

## De setup-wizard

Bij de eerste keer openen loop je deze stappen door (je kunt stappen overslaan en later invullen):

1. **Wachtwoord:** minstens 10 tekens, opgeslagen als scrypt-hash.
2. **Discord-bot-token** + Test-knop (controleert ook of de Message Content Intent aanstaat).
3. **OpenRouter-API-key** + Test-knop.
4. **Server-ID, kanaal-ID's** (`#hulp`, `#geheugen`) en **jouw user ID** (zodat de bot jou kan taggen).
   Laat je de kanaal-ID's leeg, dan gebruikt de bot de kanalen met de naam `#hulp` en `#geheugen`.
5. **Over je vader:** naam, aanspreekvorm, tv, streamingdiensten, Windows/Mac, **taal van zijn pc**
   (bepaalt of de bot Nederlandse of Engelse menunamen gebruikt), browser, wifi/router, Twitch-kanaal,
   OBS-scenes en bronnen, StreamElements, microfoon en camera.
6. **Klaar.**

Alles is later te wijzigen. De wizard kun je opnieuw openen via **Instellingen**.

---

## Toegang tot het dashboard

De poort is standaard alleen op de server zelf bereikbaar (`127.0.0.1:8080`). Kies een van deze
manieren:

### SSH-tunnel (niets extra's nodig)

Op je eigen pc of laptop:

```bash
ssh -L 8080:127.0.0.1:8080 gebruiker@jouw-server
```

Open daarna <http://localhost:8080> in je browser. Zolang het SSH-venster openstaat, werkt het.

### Tailscale (handig op je telefoon)

Installeer Tailscale op de server en je telefoon. Op de server:

```bash
sudo tailscale serve --bg 8080
```

Het dashboard staat dan op `https://<servernaam>.<jouw-tailnet>.ts.net`, met HTTPS en alleen
bereikbaar voor jouw apparaten. De compose-file hoeft niet te veranderen.

### Reverse proxy met HTTPS (vanaf internet)

Alleen doen als je het dashboard echt publiek wilt hebben. Voorbeeld met Caddy op de host (regelt
zelf een Let's Encrypt-certificaat):

```
hulp.jouwdomein.nl {
    reverse_proxy 127.0.0.1:8080
}
```

Bij nginx: `proxy_pass http://127.0.0.1:8080;` met `proxy_set_header Host $host;` en
`proxy_set_header X-Forwarded-Proto $scheme;` (dan krijgt het sessiecookie de `Secure`-vlag).
Overweeg extra bescherming (bijv. IP-allowlist of SSO), want het dashboard bevat de gesprekken van je
vader. Inloggen is beveiligd met ratelimiting (zie hieronder), maar een sterk wachtwoord blijft
belangrijk.

---

## Updaten, vastzetten en terugrollen

**Updaten** naar de nieuwste versie (na een push naar `main` en een geslaagde workflow-run):

```bash
cd ~/hulpdashboard
```

```bash
docker compose pull && docker compose up -d
```

Je data blijft bewaard: database, afbeeldingen en sleutel staan in named volumes, niet in de image.
Welke versie draait, zie je in het dashboard onder **Status** (de git-commit) en onderaan elke pagina.

**Een specifieke versie vastzetten:** elke build krijgt ook een tag met de korte commit-SHA. Zoek de
SHA op in GitHub (tabblad Actions of de commitlijst, eerste 7 tekens) en zet in `docker-compose.yml`:

```yaml
    image: ghcr.io/iitssjstn/hulpdashboard:3f9c2ab
```

Daarna:

```bash
docker compose up -d
```

**Terugrollen als een update stuk is:** zet op dezelfde manier de SHA van de vorige, goede versie
in de compose-file en draai `docker compose up -d`. Welke versie eerder draaide, zie je in de
"Samenvatting" van de workflow-runs op GitHub. Wil je later weer automatisch de nieuwste, zet dan
`:latest` terug.

> Het databaseschema wordt alleen uitgebreid (nooit afgebroken), dus terugrollen naar een iets oudere
> versie werkt in de praktijk prima. Maak voor de zekerheid eerst een [back-up](#back-up).

---

## Automatisch updaten met Watchtower (optioneel)

In `docker-compose.yml` staat onderaan een **uitgecommentarieerd** Watchtower-blok. Standaard staat
het UIT: dan bepaal jij wanneer er wordt geüpdatet.

Aanzetten:

1. Haal in `docker-compose.yml` de `#` weg voor het hele `watchtower:`-blok **en** voor de twee
   `labels:`-regels bij de `hulpdashboard`-service.
2. Draai `docker compose up -d`.

Watchtower kijkt dan elke nacht om 04:00 of er een nieuwe `latest` is, en herstart de container
daarmee. Het label zorgt dat hij alleen dít project bijwerkt en niet al je andere containers.

Aandachtspunten:

- Watchtower heeft toegang tot de Docker-socket, en dat is in feite root-toegang tot je server.
- Het origineel (`containrrr/watchtower`) wordt nauwelijks meer onderhouden. Op Docker 29+ is
  `DOCKER_API_VERSION=1.44` nodig (staat al in het blok). Controleer of er een beter onderhouden
  alternatief is als je hem gebruikt.
- Privé package (optie B hierboven)? Mount dan je Docker-login in Watchtower (de regel met
  `config.json` staat klaar in het blok).
- Vastgezet op een SHA-tag? Dan doet Watchtower niets, en dat is precies wat je dan wilt.

---

## Beveiliging: wat wel en niet beschermd is

**Hoe het werkt**

- Er staan **geen geheimen** in de code, de image, git, de compose-file of een `.env`. Je vult ze
  alleen in het dashboard in.
- Discord-token en OpenRouter-key worden **versleuteld** opgeslagen in SQLite (Fernet: AES-128-CBC +
  HMAC-SHA256).
- De sleutel wordt bij de eerste start automatisch gemaakt in `/keys/secret.key` (chmod 600, map 700),
  in een **apart volume** (`hulp-keys`), los van de database (`hulp-data`).
- Opgeslagen geheimen worden **nooit meer getoond**, alleen "ingesteld ✓" met een knop om te vervangen.
- Het dashboardwachtwoord wordt opgeslagen als **scrypt-hash** (met salt).
- **Login-ratelimiting:** na 5 foute pogingen binnen 15 minuten is een IP 15 minuten geblokkeerd, en
  daarna steeds langer (tot 24 uur). Na 20 foute pogingen in totaal binnen 15 minuten gaat inloggen voor
  iedereen 15 minuten dicht.
- Sessies: willekeurig token (alleen de hash staat in de database), `HttpOnly`- en `SameSite=Strict`-cookie,
  CSRF-token op elk formulier, en strikte security-headers (CSP zonder inline scripts, geen iframes).
  Na het wijzigen van je wachtwoord worden alle andere sessies uitgelogd.
- De container draait als **niet-root** (uid 10001), met een read-only bestandssysteem, zonder
  Linux-capabilities en met `no-new-privileges`.
- De bot vraagt nooit om wachtwoorden of bankgegevens, poetst ze weg uit tekst voordat er iets wordt
  opgeslagen of verstuurd, en slaat nooit feiten op waar ze in staan.

**Wat dit WEL beschermt**

- Een gelekte of gedeelde **database-back-up** (bijv. alleen het `hulp-data`-volume) bevat geen
  bruikbare tokens zonder de sleutel uit het andere volume.
- Geheimen belanden niet per ongeluk in git, in de image op ghcr.io, in `docker inspect`, in
  omgevingsvariabelen of in de logs.
- Een gestolen image is waardeloos voor een aanvaller: daar staan geen geheimen in.

**Wat dit NIET beschermt**

- Iemand met **root of Docker-toegang op je server** kan beide volumes lezen, en kan dus de geheimen
  ontsleutelen. Hetzelfde geldt voor iemand die code in de container kan uitvoeren. De sleutel moet
  immers op de server staan, anders kan de bot niet automatisch starten. Dit is versleuteling "at rest",
  geen kluis.
- Een back-up met **beide** volumes erin is net zo gevoelig als de geheimen zelf.
- **Gesprekken, afbeeldingen en het geheugen** zijn niet versleuteld (alleen de geheimen). Behandel de
  data-volume dus als privé.
- **Wat je vader op een foto zet** (bijv. een screenshot met zijn bankapp) kan de bot niet wegpoetsen.
  Die afbeelding gaat naar het model en wordt bewaard. Gratis modellen bij OpenRouter kunnen invoer
  gebruiken of loggen, afhankelijk van de provider. Je kunt dat beperken via de privacy-instellingen
  van je OpenRouter-account.
- Het herkennen van gevoelige onderwerpen gebeurt met trefwoorden plus het oordeel van het model. Dat
  is goed, maar niet waterdicht. Extra trefwoorden kun je toevoegen bij **Instellingen**.
- Het dashboard heeft één gebruiker. Wie het wachtwoord heeft, kan alles.

---

## Back-up

Stop eerst de container (voor een consistente SQLite-kopie), maak dan van beide volumes een archief:

```bash
docker compose stop
```

```bash
docker run --rm -v hulpdashboard_hulp-data:/data -v "$PWD":/backup busybox tar czf /backup/hulp-data.tgz -C /data .
```

```bash
docker run --rm -v hulpdashboard_hulp-keys:/keys -v "$PWD":/backup busybox tar czf /backup/hulp-keys.tgz -C /keys .
```

```bash
docker compose start
```

Bewaar `hulp-keys.tgz` **op een andere plek** dan `hulp-data.tgz`. Ben je de sleutel kwijt? Dan blijven
gesprekken en geheugen gewoon werken, maar moet je het Discord-token en de OpenRouter-key opnieuw
invullen in het dashboard.

---

## Gebruik in Discord

In `#hulp` (en draadjes daarin) gewoon een vraag typen of een afbeelding plakken. Commando's:

| Commando | Wat het doet |
|---|---|
| `!onthoud <tekst>` | Bot onthoudt dit feit (geweigerd als er een wachtwoord/bankgegeven in staat) |
| `!geheugen` | Laat genummerd zien wat de bot onthouden heeft |
| `!vergeet <nummer of tekst>` | Verwijdert een feit (bij meerdere treffers vraagt hij welke) |
| `!hulp` | Korte uitleg |

In `#geheugen` meldt de bot nieuwe feiten. In de modus "eerst goedkeuren" klik jij (alleen jouw user ID
telt) op ✅ of ❌, of je doet het in het dashboard onder **Geheugen**.

---

## Ontwikkelen en testen

Lokaal (Python 3.12):

```bash
python -m venv .venv && . .venv/bin/activate
```

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest -q
```

De tests draaien zonder echte tokens: OpenRouter en Discord worden nagebootst. Ze dekken onder andere
de fallback tussen modellen, het herkennen en wegpoetsen van gevoelige gegevens, geheugen en
commando's, de setup-wizard, CSRF, login-ratelimiting, het verbergen van geheimen en het opnieuw
verbinden van de bot na een tokenwissel. Dezelfde tests draaien in GitHub Actions vóór elke build;
faalt er een test, dan wordt er geen image gepubliceerd.

Lokaal de app draaien zonder Docker (data in `./data` en `./keys`, staan in `.gitignore`):

```bash
HULP_DATA_DIR=./data HULP_KEY_DIR=./keys python -m app
```

---

## Projectstructuur

```
.
├── app/
│   ├── __main__.py        # start uvicorn (dashboard + bot)
│   ├── main.py            # FastAPI-routes: wizard, pagina's, API voor Test-knoppen
│   ├── bot.py             # Discord-laag + BotManager (opnieuw verbinden, status, health)
│   ├── core.py            # bot-logica zonder Discord: antwoorden, commando's, geheugen
│   ├── ai.py              # OpenRouter-client met fallback, key-test, modellijst
│   ├── prompt.py          # standaard-systeemprompt + profiel + feiten
│   ├── memory.py          # feiten opslaan, inkorten voor de prompt, extractie-prompt
│   ├── sensitive.py       # gevoelige onderwerpen herkennen en gegevens wegpoetsen
│   ├── auth.py            # wachtwoord-hash, sessies, login-ratelimiting
│   ├── settings.py        # instellingen + versleutelde geheimen
│   ├── crypto.py          # Fernet-sleutel aanmaken/laden
│   ├── db.py              # SQLite-schema en helpers
│   ├── discord_check.py   # token/intent/kanaal-controle voor de Test-knop
│   ├── logsetup.py        # logs naar stdout + waarschuwingen naar de Status-pagina
│   ├── healthcheck.py     # Docker-healthcheck
│   ├── templates/         # Jinja2-HTML
│   └── static/            # CSS, JS, icoon
├── tests/                 # pytest
├── .github/workflows/docker.yml
├── Dockerfile
├── docker-compose.yml
├── requirements.txt       # runtime (vaste versies)
└── requirements-dev.txt   # alleen voor tests
```
