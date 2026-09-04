# 🧠 CodeGraph Explorer — Script-First (v4.5)

Skill pour Claude Code, inspiré de **Graphify** et **Understand Anything**.

## 🎯 Philosophie : Zero Token Waste

> **Claude ne parse jamais de code source. Il exécute un script Python local, puis lit le JSON.**

| Avant (v2, cassé) | Après (v3) |
|---|---|
| `codegraph_builder.py` contenait une erreur de syntaxe Python (5 lignes) et plantait avant même de démarrer | Le script compile et tourne, testé sur un mini-projet multi-langage et sur 206 fichiers réels de la stdlib Python |
| Le mode watch dupliquait tous les edges à chaque rebuild | État reconstruit à neuf depuis un cache par fichier à chaque build — jamais de doublons |
| `--report` et `--update` ne faisaient rien de différent d'un build complet | `--update` ne reparse que les fichiers modifiés ; `--report` régénère le rapport sans toucher aux sources |
| Les edges `calls` scannaient tout le fichier, attribuant à chaque fonction tous les appels du fichier | Chaque fonction n'est scannée que dans son propre corps, et la confiance de l'edge reflète l'ambiguïté du nom |
| C/C++/Ruby/PHP/Swift/Kotlin annoncés mais aucune extraction réelle | Support réel pour les 12 langages listés plus bas |
| `.gitignore`, redaction des secrets, `custom_patterns.json` documentés mais jamais implémentés | Implémentés (voir portée exacte dans `SKILL.md`) |
| « Claude lit graph.json » présenté comme ~200 tokens quelle que soit la taille du projet | Mesuré : 31,7 Mo sur 206 fichiers stdlib (~10 500 nœuds) — donc `--explain`/`--callers`/`--find-path` côté script (v3.1, ci-dessous), qui répondent en < 1s sans jamais faire lire le JSON entier à Claude |
| `graph.html` : tooltip au survol seulement, pas de recherche, illisible au-delà de quelques centaines de nœuds | Panneau latéral cliquable, filtre par type, recherche par nom, plafond de rendu (top 700 par degré, ajustable) sur les gros graphes (v3.1) |
| `require()` dans un fichier `.ts` était invisible (le pattern existait pour `.js` mais pas pour `.typescript` dans le dict interne) alors qu'il fonctionnait déjà en `.js` | Corrigé (v3.2) — trouvé en testant un vrai petit projet TypeScript (Express + `require()`), pas par relecture |
| `const Foo = () => {}` (la façon dont s'écrit la plupart des composants React et du code service/handler Node) était invisible sauf si la ligne matchait aussi `export const X = ...` | Capturé comme n'importe quelle fonction, exporté ou non (v3.3) |
| `confidence` d'un edge `calls` ne dépendait que du nombre de candidats portant le même nom | +0.25 (plafonné à 0.95) quand le fichier appelant importe réellement un fichier qui ressemble à celui du candidat précis — un indice réel, pas juste une coïncidence de nom (v3.3) |
| « Trouve les points d'entrée de X » demandait de rappeler `--callers` à la main sur chaque appelant jusqu'à tomber sur un entrypoint | `--trace-entrypoints X` fait cette remontée en un seul appel (v3.3) |
| Les entrypoints JS/TS/Ruby/PHP/Java étaient documentés « corps = fichier entier » mais le scan démarrait en réalité à la ligne du marqueur (`app.listen(...)`, `if __FILE__ == $0`...) jusqu'à la fin — or ce marqueur est typiquement la *dernière* ligne d'une appli Express, donc presque tout ce que fait l'entrypoint était raté | Corrigé : scan du vrai fichier entier, tout en excluant toujours la ligne du marqueur elle-même pour éviter l'auto-référence qui avait causé un faux positif similaire sur Java/Go/Rust une première fois (v3.3) — trouvé en testant `--trace-entrypoints` sur une vraie chaîne d'appels à plusieurs niveaux |
| Un bug de `.gitignore` a un jour fait planter `discover()` à « 0 fichier trouvé » sur un vrai projet, ce qui aurait pu écraser silencieusement un `graph.json` correct par un graphe vide | Shrink-guard (v3.4) : `build()` refuse désormais de continuer si `discover()` trouve moins de 10 % des fichiers suivis par `.file_cache.json`, avec `--force-rebuild` pour passer outre quand la baisse est réelle et voulue |
| « 0 token LLM » était une promesse implicite, jamais affichée noir sur blanc au moment du build | Chaque build logge désormais `LLM tokens spent on this build: 0`, et l'en-tête de `GRAPH_REPORT.md` porte la même mention (v3.4) |
| Exclure quelque chose du graphe sans l'exclure de git obligeait à modifier `.gitignore` lui-même | `.codegraphignore` (v3.4) : même syntaxe, fichier séparé à la racine, fusionné avec `.gitignore` |
| « Qu'est-ce qui casse si je change X ? » demandait de rappeler `--callers` récursivement à la main | `--impact X` (v3.4) : fermeture transitive complète (pas un seul saut), avec les entrypoints impactés signalés à part |
| Voir le graphe dans un vrai outil de visualisation de graphe (Obsidian, très demandé chez Graphify) n'était pas possible sans script externe | `--export-obsidian` (v3.4) : une note Markdown par nœud, `[[wikilinkée]]`, dans `.codegraph/obsidian_vault/` — vue graphe native d'Obsidian, zéro plugin |
| Les méthodes de classe JS/TS/Java étaient invisibles : par regex, `nom(...) {` est indiscernable d'un `if (...) {` ou d'un raccourci de méthode d'objet littéral, donc jamais extrait de façon fiable | Moteur **tree-sitter** optionnel (v4.0, `pip install tree-sitter tree-sitter-javascript tree-sitter-typescript tree-sitter-java`) : un vrai arbre de syntaxe distingue sans ambiguïté un `method_definition`/`method_declaration` — méthodes de classe extraites (avec `private`/`protected`/getter/setter/static/constructeur), edges `calls` désormais résolus même à l'intérieur d'un corps de méthode. Fallback regex automatique par fichier si le paquet manque ou si le parsing échoue — comportement v3 inchangé dans ce cas |
| Répondre à une question multi-sauts ou une agrégation sur le graphe demandait d'enchaîner plusieurs `--callers`/`--find-path` à la main | **Ladybug/Cypher** en couche parallèle, optionnelle (v4.0, `pip install ladybug`) : `--sync-graphdb` exporte `graph.json` vers `.codegraph/graph_db/`, `--cypher "<requête>"` exécute du vrai Cypher dessus — **s'ajoute** au moteur JSON + BFS/Dijkstra existant, ne le remplace jamais ; `--explain`/`--callers`/`--find-path`/`--trace-entrypoints`/`--impact` restent le chemin par défaut, sans dépendance |
| Seul le `.gitignore`/`.codegraphignore` à la racine du projet était lu — un vrai monorepo avec des règles par sous-paquet n'était pas couvert | `.gitignore`/`.codegraphignore` imbriqués (v4.1) : tout fichier trouvé sous le projet contribue ses propres motifs, portés à son propre dossier (jamais de fuite vers un dossier voisin sans son propre fichier) |
| `.codegraph/graph_db/` n'était jamais resynchronisé automatiquement après un rebuild, et rien ne le signalait — une requête `--cypher` pouvait répondre en silence depuis un graphe périmé | Avertissement de fraîcheur (v4.1) : `--cypher` compare désormais le `graph.json` du dernier `--sync-graphdb` à l'actuel et imprime `graph_db/ may be stale -- ...` s'il est périmé — informatif, ne bloque jamais la requête |
| Relancer `--sync-graphdb` une deuxième fois sur un `graph_db/` déjà existant plantait (`NotADirectoryError`) | Corrigé (v4.1) — cette version de Ladybug stocke la base comme un fichier unique, pas un dossier ; le nettoyage gère les deux cas |
| Aucune suite de tests automatisée — toute vérification se faisait à l'œil, en session, à chaque changement | Suite pytest (v4.1, `tests/`) couvrant le shrink-guard, le cache incrémental, les 3 bugs tree-sitter trouvés en construisant v4.0, le fallback tree-sitter→regex, le piège Cypher, l'avertissement de fraîcheur et sa correction, les commandes de requête, l'export Obsidian |
| tree-sitter (v4.0) ne couvrait que JS/TS/Java — Go, Rust, C, C++ et PHP restaient sur le moteur regex (pas de méthodes, pas de `bases`/`inherits` pour C++/PHP, imports groupés Go/Rust invisibles) | tree-sitter étendu à **Go/Rust/C/C++/PHP** (v4.2, chaque paquet de grammaire optionnel et indépendant) : méthodes extraites pour les 5, `inherits` pour Rust (`impl Trait for Type`, même fichier uniquement) et PHP (`extends`+`implements`) et C++ (`class X : public Base`), imports groupés Go (`import (...)`) et Rust (`use ... {...}`) enfin capturés, signatures C/C++ multi-lignes capturées. Fallback regex automatique par fichier si le paquet manque ou si le parsing échoue, comme pour JS/TS/Java |
| `confidence` d'un edge `calls` ne reflétait que le nombre de candidats homonymes *dans tout le projet*, jamais où le candidat vivait réellement par rapport à l'appelant | Deux tiers de résolution réelle, tag `RESOLVED` (v4.3) : **même fichier** (un seul candidat homonyme défini dans le fichier de l'appelant, `confidence: 0.97`, tous langages) et **import vérifié sur le vrai système de fichiers** (pas une ressemblance de nom, `confidence: 0.93`, imports relatifs JS/TS et chemins de module Python en pointillés). Sinon, retombe sur l'ancien barème `INFERRED`, mais désormais compté sur l'ensemble déjà réduit par la résolution d'import quand elle ne tranche pas complètement |
| Les communautés étaient toujours un regroupement par dossier, même quand la vraie relation fonctionnelle traversait plusieurs dossiers (un handler et le service qu'il appelle systématiquement, par exemple) | Clustering **Leiden** optionnel sur les edges `calls`/`inherits` (v4.4, `--community-algo {auto,directory,leiden}`, `pip install python-igraph leidenalg` — wheels précompilées, aucun compilateur requis) : `auto` (défaut) l'utilise si installé, sinon retombe sur l'ancienne heuristique par dossier, sans jamais changer la forme de `communities` en sortie |
| Aucun moyen de savoir ce qu'un build a changé, ni de comparer l'état du graphe à un point de référence choisi | `--diff [nom]` + `--snapshot <nom>` (v4.4) : `--diff` sans nom compare au build précédent (rotation automatique, zéro préparation) ; `--snapshot`/`--diff <nom>` compare à un point nommé explicitement, qui survit à autant de builds qu'on veut. Limite documentée : un symbole qui n'a fait que *bouger* (id de nœud incluant son numéro de ligne) apparaît en `removed`+`added`, jamais en `changed` |
| Le scanner de secrets ne couvrait que 3 formes (mot-clé=valeur, clé AWS, bearer token) | Redaction élargie (v4.4) : préfixes GitHub/GitLab/Slack/Stripe/npm documentés, webhooks Slack, marqueurs de clé privée PEM, JWT (préfixe `eyJ` quasi certain), et un détecteur générique par entropie de Shannon pour un secret sans mot-clé ni préfixe reconnu — calibré empiriquement contre des valeurs réalistes non sensibles (hash sha256, UUID, identifiant camelCase, chaîne de version) pour éviter les faux positifs plutôt que de deviner un seuil |
| Entre v3.4 et v4.4, `graph.html` avait régressé en rechargeant D3 depuis `https://d3js.org` — page blanche à l'ouverture hors-ligne, alors que le `SKILL.md` continuait de le qualifier de « self-contained » | Renderer sans dépendance restauré (v4.5) : petite simulation de forces vélocité-Verlet faite main, SVG à la main, pan/zoom/glisser/recherche/légende en vanilla, aucun `<script src>`, aucun CDN. Récupère aussi le focus sur les voisins que la version D3 avait perdu. Vérifié avec `window.d3 === undefined` et zéro erreur console |
| Les edges `calls` JS/TS venaient d'un scan `\bnom(` du texte du corps : `if (`/`while (`/`catch (` comptés comme des appels, `nom(` en commentaire aussi, méthodes privées ES `#m()` jamais vues, récepteur (`this`, `db`, …) ignoré | Résolution `calls` par AST tree-sitter (v4.5, JS/TS) : chaque `call_expression`/`new_expression` réel devient un call site `{name, recv, line}` attribué à la fonction englobante ; `this.m()` se résout vers une méthode de la classe de l'appelant (`resolved_by: "this_method"`, 0.97). Mesuré sur `sindresorhus/got` : +~30 edges de méthodes privées réels, ~115 appels passés d'un `same_file` vague à un `this_method` précis, ~9 faux positifs en commentaire supprimés. Les autres langages gardent le scan de corps inchangé |
| Le cache incrémental était indexé sur `mtime` seul : `git checkout`, `git stash pop`, `rsync`, `touch` bougent l'horodatage sans toucher au contenu → tout le projet reparsé pour rien | Clé secondaire par hash de contenu (v4.5) : `.file_cache.json` stocke un `sha` sha256 ; un `mtime` périmé mais un `sha` identique → servi du cache, `mtime` rafraîchi pour la fois d'après. Le hash n'est calculé que sur le chemin lent (mtime déjà différent) — un arbre inchangé ne coûte rien de plus |
| `--impact X` mélangeait code prod impacté et tests dans une seule liste — impossible de voir d'un coup d'œil « quels tests relancer » | `--impact` séparé prod / test (v4.5, #D) : chaque nœud `file` porte `metadata.role` (`"test"` si segment de dossier `tests/`/`spec/`/… ou nom `test_*.py`/`*_test.go`/`*.test.ts`/… , sinon `"prod"`) ; `--impact` sort la liste détaillée en prod seul + un champ `tests_to_run` listant les fichiers de test qui exercent transitivement le symbole modifié |
| « Quel fichier dépend de quel fichier ? » obligeait à parcourir des milliers de nœuds `function` | Edges `file → file` agrégés (v4.5, #C) : les edges `calls`/`inherits` sont repliés en une arête pondérée par paire (fichier source → fichier cible), stockées à part dans `graph.json["file_deps"]` (pas mélangées aux `edges`). Commande `--file-deps [FICHIER|SYMBOLE]` — dépendances entrantes/sortantes d'un fichier, ou la liste complète triée par poids. Lookup sur ~quelques centaines d'entrées, instantané |

Le détail complet des bugs trouvés et corrigés est dans `SKILL.md` (§ "What changed in v3" … "What changed in v4.5").

## 🚀 Installation

```bash
unzip codegraph-explorer-skill.zip -d ~/.claude/skills/codegraph
# Redémarre Claude Code
```

## ✨ Comportement proactif & token-free

### Premier contact
Claude détecte un projet → exécute `codegraph_builder.py` → lit le JSON.

```
Graph built. 1247 nodes, 3892 edges.
```

### Questions architecture
Pour une question ciblée, Claude appelle `--explain`/`--callers`/`--find-path`/
`--trace-entrypoints`/`--impact` (le script fait la recherche, Claude ne lit que le
résultat) ; pour une vue d'ensemble, il lit `GRAPH_REPORT.md`. Il ne charge
`.codegraph/graph.json` en entier que rarement, et **jamais** les fichiers sources.

### Mise à jour
Code modifié → `codegraph_builder.py --update` (ou mode watch) → seuls les fichiers
modifiés sont reparsés, le JSON est rafraîchi sans doublons.

## Commandes

```
/graph build               # Rebuild complet (ignore le cache)
/graph build --watch       # Mode continu (incrémental, surveillance fichiers)
/graph build --force       # Ignore le shrink-guard (voir plus bas) et force le rebuild
/graph explain UserService # --explain côté script : métadonnées + edges entrants/sortants
/graph callers UserService # --callers côté script : qui appelle/hérite de ce symbole
/graph path auth payment   # --find-path côté script : plus court chemin pondéré
/graph entrypoints UserService # --trace-entrypoints côté script : entrypoints les plus proches
/graph impact UserService  # --impact côté script : fermeture transitive (blast radius) + prod/test + tests à relancer
/graph file-deps core/options.ts  # --file-deps : dépendances fichier→fichier (calls/inherits agrégés)
/graph file-deps           # sans argument : toutes les dépendances fichier→fichier, les plus lourdes d'abord
/graph query "..."         # NL → Claude choisit quelle(s) commande(s) ci-dessus lancer
/graph report              # Régénère GRAPH_REPORT.md depuis le JSON existant, sans reparser
/graph obsidian             # Régénère .codegraph/obsidian_vault/ depuis le JSON existant
/graph sync-graphdb        # Exporte le graphe vers .codegraph/graph_db/ (Ladybug, optionnel)
/graph cypher "..."        # Requête Cypher arbitraire sur graph_db/ (nécessite sync-graphdb)
/graph snapshot before-refactor  # Checkpoint nommé du graph.json actuel (v4.4)
/graph diff                # Ce qui a changé depuis le dernier build (v4.4)
/graph diff before-refactor      # Ce qui a changé depuis ce snapshot nommé (v4.4)
```

## Architecture

```
┌─────────────┐     exécute une fois      ┌──────────────────┐
│   Claude    │ ───────────────────────→ │ codegraph_builder │
│  (tokens)   │                          │   .py (local)     │
│             │ ←─────────────────────── │                    │
│             │    lit graph.json         │  ast / regex       │
│             │    (~200 tokens)          │  cache incrémental │
└─────────────┘                          └──────────────────┘
```

## Structure du skill

```
codegraph/
├── SKILL.md                         ← Instructions Claude (Script-First)
├── README.md                        ← Ce fichier
├── scripts/
│   └── codegraph_builder.py         ← Script local (stdlib only, watchdog/tree-sitter/ladybug optionnels)
├── references/
│   ├── graph_schema.md              ← Schéma JSON + schéma graph_db/ (Ladybug/Cypher)
│   ├── extraction_patterns.md       ← Patterns par langage (+ tree-sitter JS/TS/Java/Go/Rust/C/C++/PHP) + limites connues
│   ├── query_protocol.md            ← Protocole de requête JSON (avec pondération par confiance)
│   └── auto_build_protocol.md       ← Règles d'activation
├── templates/
│   └── graph_report.md              ← Template du rapport (réellement utilisé par le script)
└── tests/                           ← Suite pytest (dev-only, voir § Tests plus bas)
    ├── conftest.py
    ├── requirements-dev.txt
    └── test_*.py
```

## Langages supportés (par le script)

- **Python** : parsing AST natif (`ast` module) — le plus précis, avec fallback regex si `SyntaxError`
- **JavaScript/TypeScript (incl. `.tsx`)** : **tree-sitter** (v4.0, optionnel — `pip install tree-sitter tree-sitter-javascript tree-sitter-typescript`) si installé — vrai AST, **méthodes de classe incluses** (private/protected/getter/setter/static/constructeur) ; sinon fallback regex non-ancrées (v3, pas d'extraction au niveau méthode)
- **Go** : **tree-sitter** (v4.2, optionnel — `pip install tree-sitter tree-sitter-go`) si installé — méthodes scopées au type receveur, imports groupés (`import (...)`) enfin capturés ; sinon fallback regex ancrées (imports groupés invisibles, pas de méthodes)
- **Rust** : **tree-sitter** (v4.2, optionnel — `pip install tree-sitter tree-sitter-rust`) si installé — méthodes (`impl`/`impl Trait for Type`, avec edge `inherits` vers le trait, **même fichier uniquement**), `use` groupés capturés en entier ; sinon fallback regex ancrées (pas de méthodes, `use` groupés tronqués)
- **Java** : **tree-sitter** (v4.0, optionnel — `pip install tree-sitter tree-sitter-java`) si installé — classes, interfaces, imports, **et méthodes/constructeurs** ; sinon fallback regex (v3 : classes/interfaces/imports uniquement, voir `references/extraction_patterns.md`)
- **C** : **tree-sitter** (v4.2, optionnel — `pip install tree-sitter tree-sitter-c`) si installé — signatures multi-lignes capturées ; sinon fallback regex terminées par une accolade (signature entière requise sur une ligne)
- **C++** : **tree-sitter** (v4.2, optionnel — `pip install tree-sitter tree-sitter-cpp`) si installé — classes, héritage (`bases`/`inherits`), méthodes ; **visibilité des membres non trackée** (tout est `is_public: true`) ; sinon fallback regex (mêmes limites que C, plus aucun héritage)
- **PHP** : **tree-sitter** (v4.2, optionnel — `pip install tree-sitter tree-sitter-php`) si installé — classes/interfaces, `extends`+`implements` → deux edges `inherits`, méthodes (visibilité/`static`) ; sinon fallback regex (pas d'héritage, pas de méthodes scopées)
- **Ruby, Swift, Kotlin** : regex ancrées en début de ligne

Tout autre langage obtient un nœud `file` (langage détecté, sans extraction de symboles),
sauf si vous ajoutez des patterns dans `.codegraph/custom_patterns.json` (voir `SKILL.md`).

## Le script local

Le script `codegraph_builder.py` est :
- **100% offline** — aucun appel réseau
- **Pur Python stdlib** — zéro dépendance obligatoire (`watchdog` optionnel pour le mode watch, sinon fallback en polling 5s ; `tree-sitter`+grammaires optionnel pour l'extraction JS/TS/Java/Go/Rust/C/C++/PHP au niveau méthode, sinon fallback regex v3 ; `ladybug` optionnel pour `--sync-graphdb`/`--cypher`, sans quoi ces deux commandes seules sont indisponibles ; `python-igraph`+`leidenalg` optionnels pour `--community-algo leiden`, sinon `auto` retombe silencieusement sur l'heuristique par dossier)
- **Extensible** — patterns additionnels dans `.codegraph/custom_patterns.json`
- **Réellement incrémental** — cache par fichier (`.codegraph/.file_cache.json`), jamais de duplication d'edges même après de nombreux rebuilds

### Dépendances optionnelles

```bash
pip install watchdog                                                          # mode watch réactif
pip install "tree-sitter>=0.23,<0.25" tree-sitter-javascript tree-sitter-typescript tree-sitter-java  # méthodes + calls AST JS/TS/Java
pip install tree-sitter-go tree-sitter-rust tree-sitter-c tree-sitter-cpp tree-sitter-php  # méthodes Go/Rust/C/C++/PHP (v4.2)
pip install ladybug                                                           # --sync-graphdb / --cypher
pip install python-igraph leidenalg                                          # --community-algo leiden (v4.4, wheels précompilées)
```
> `tree-sitter` **0.26 segfault** en cours de parsing d'un vrai arbre TS avec les
> grammaires actuelles ; d'où la borne `<0.25`. La 0.23.x est la dernière ligne contre
> laquelle ce code a été vérifié.
Chacune est évaluée indépendamment à l'import : l'absence d'une seule (ou de plusieurs)
ne désactive jamais le reste du script — voir `SKILL.md` § "What changed in v4.0" pour
le détail du fallback par langage/commande.

### Utilisation standalone

```bash
python codegraph_builder.py /chemin/vers/projet             # build complet
python codegraph_builder.py --update /chemin/vers/projet    # incrémental
python codegraph_builder.py --watch /chemin/vers/projet     # continu
python codegraph_builder.py --report /chemin/vers/projet    # régénère juste le rapport
python codegraph_builder.py --export-obsidian /chemin/vers/projet  # régénère le vault Obsidian
python codegraph_builder.py --sync-graphdb /chemin/vers/projet     # exporte vers .codegraph/graph_db/ (Ladybug)
python codegraph_builder.py --force-rebuild /chemin/vers/projet    # bypasse le shrink-guard
python codegraph_builder.py --verbose /chemin/vers/projet   # diagnostics détaillés

# requêtes en lecture seule sur le graph.json existant (rien n'est reparsé) :
python codegraph_builder.py /chemin/vers/projet --explain AuthService
python codegraph_builder.py /chemin/vers/projet --callers AuthService.login
python codegraph_builder.py /chemin/vers/projet --find-path AuthService PaymentGateway
python codegraph_builder.py /chemin/vers/projet --trace-entrypoints AuthService.login
python codegraph_builder.py /chemin/vers/projet --impact AuthService.login   # blast radius complet
python codegraph_builder.py /chemin/vers/projet --explain AuthService --json   # sortie structurée
python codegraph_builder.py /chemin/vers/projet --cypher "MATCH (n:Symbol) RETURN n.name LIMIT 5"  # nécessite --sync-graphdb au préalable
```

## Outputs

| Fichier | Description |
|---------|-------------|
| `.codegraph/graph.json` | Graphe complet (machine-readable) |
| `.codegraph/GRAPH_REPORT.md` | Résumé humain, généré depuis `templates/graph_report.md` |
| `.codegraph/graph.html` | Exploration interactive **auto-contenue** (rendu force sans dépendance : plus de D3/CDN, s'ouvre hors-ligne) : panneau latéral au clic avec focus sur les voisins, recherche par nom, filtre par type, glisser un nœud pour l'épingler. Pour l'utilisateur, pas pour Claude — aussi volumineux que `graph.json` |
| `.codegraph/.file_cache.json` | Cache interne par fichier (mtime + `sha` sha256 du contenu + nœuds/edges extraits) — sert au mode incrémental et au shrink-guard |
| `.codegraph/obsidian_vault/` | Une note Markdown par nœud, `[[wikilinkée]]` — généré uniquement sur demande (`--export-obsidian`), jamais par un build normal |
| `.codegraph/graph_db/` | Base de graphe embarquée Ladybug (tables génériques `Symbol`/`Edge`), interrogeable en Cypher — généré uniquement sur demande (`--sync-graphdb`), jamais par un build normal ; nécessite `pip install ladybug` |

## Limites connues (honnêtement documentées, pas cachées)

- Les edges `calls` restent fondamentalement une résolution par nom, pas une vraie
  résolution de portée/type : deux symboles sans rapport portant le même nom peuvent en
  théorie se retrouver reliés. La *liste* des appels d'une fonction vient d'un vrai AST
  pour JS/TS (nœuds `call_expression`/`new_expression` — plus de faux `if (`/`while (`,
  plus de `nom(` en commentaire, méthodes privées `#m()` visibles, récepteur capturé)
  et d'un scan `\bnom(` du texte du corps partout ailleurs. Côté résolution, trois
  tiers `RESOLVED` passent devant l'heuristique quand ils s'appliquent — `this.m()`
  vers une méthode de la classe de l'appelant (`resolved_by: "this_method"`, `0.97`,
  JS/TS), même fichier (`resolved_by: "same_file"`, `0.97`, tous langages), et import
  résolu sur le vrai système de fichiers (`resolved_by: "import"`, `0.93`, JS/TS
  relatifs et chemins Python en pointillés seulement — voir `references/query_protocol.md`).
  Ailleurs (Go/Rust/Java/PHP/C/C++, ou tout import que la résolution ne parvient pas à
  faire correspondre), le champ `confidence` d'un edge `tag: INFERRED` reflète le
  nombre de candidats homonymes restants (0.85 si unique dans l'ensemble considéré,
  jusqu'à 0.2 si très répandu), plus un bonus `+0.25` (plafonné à 0.95, depuis v3.3)
  quand le fichier appelant importe quelque chose qui *ressemble* au fichier du
  candidat précis (comparaison par nom de fichier, pas résolution réelle) — à pondérer
  en conséquence plutôt qu'à ignorer, et à ne jamais traiter comme une preuve.
- `.gitignore`/`.codegraphignore` imbriqués (v4.1) : chaque fichier trouvé sous le
  projet applique ses propres motifs à son propre dossier ; une négation (`!motif`)
  ne joue que dans son propre fichier — un fichier imbriqué ne peut pas « désignorer »
  quelque chose qu'un fichier plus haut dans l'arborescence a déjà exclu. Toujours pas
  spec-complete sur `**`/certains cas `!` complexes, comme le fichier racine avant.
- La redaction de secrets est une passe regex best-effort sur de courts extraits, pas un
  scanner de secrets complet.
- Java / JS/TS, **sans tree-sitter installé** (comportement v3, toujours le fallback si
  `tree-sitter`/les paquets de grammaire manquent, ou si le parsing d'un fichier
  échoue) : uniquement classes/interfaces/imports (+ déclarations top-level pour JS/TS),
  pas les méthodes — regex fiable impossible sans vrai parseur, fort taux de faux
  positifs sur les getters/setters ou un `if (...) {` confondu avec une méthode. Les
  appels *à l'intérieur* d'une méthode de classe n'apparaissent alors pas dans
  `--callers`. **Avec tree-sitter installé** (v4.0 — `pip install tree-sitter
  tree-sitter-javascript tree-sitter-typescript tree-sitter-java`), cette limite est
  levée : un vrai arbre de syntaxe extrait les méthodes de classe sans ambiguïté
  (private/protected/getter/setter/static/constructeur inclus) et les edges `calls`
  résolvent aussi depuis l'intérieur d'un corps de méthode. Voir
  `references/extraction_patterns.md` pour le détail par type de nœud.
- Go/Rust/C/C++/PHP, **sans tree-sitter installé pour ce langage** : mêmes limites que
  Java/JS/TS sans tree-sitter (voir juste au-dessus) — signatures multi-lignes ratées
  pour C/C++, imports groupés invisibles pour Go/Rust, pas de méthodes ni d'héritage
  pour aucun des cinq. **Avec tree-sitter installé** (v4.2 — `pip install
  tree-sitter-go tree-sitter-rust tree-sitter-c tree-sitter-cpp tree-sitter-php`, tout
  sous-ensemble), voir `references/extraction_patterns.md` pour le détail par langage ;
  deux limites documentées y subsistent même avec tree-sitter installé : la liaison
  `impl Trait for Type` → `inherits` en Rust ne fonctionne que **si le trait et la
  struct sont dans le même fichier** (pas de résolution inter-fichiers pour l'instant),
  et la visibilité des membres C++ (`public:`/`private:`) n'est pas trackée du tout
  (toutes les méthodes de classe sont `is_public: true` par défaut).
- `--find-path` traite le graphe comme non orienté et pénalise les arêtes `contains` par
  rapport à `calls`/`inherits`, pour préférer une vraie relation de code à « ces deux
  symboles sont dans le même fichier » — mais reste un plus-court-chemin pondéré, pas
  une explication causale.
- `graph.html` ne simule que les ~700 nœuds de plus haut degré par défaut sur un gros
  projet (bannière + champ pour ajuster) ; les données de tous les nœuds restent
  présentes dans le fichier, seule la mise en page initiale est plafonnée.
- `--impact` hérite de l'imprecision heuristique des edges `calls` (voir plus haut) —
  plus la profondeur de la fermeture transitive augmente, plus les faux positifs
  peuvent s'accumuler d'un saut à l'autre ; regarder la `confidence` de chaque saut,
  pas seulement le nombre total de nœuds impactés.
- Le shrink-guard (seuil : moins de 10 % des fichiers précédemment suivis, à partir de
  5 fichiers suivis) est lui aussi une heuristique, pas une détection de bug garantie :
  une vraie suppression massive et volontaire de fichiers le déclenchera aussi — c'est
  pour ça que `--force-rebuild` existe plutôt que de bloquer sans échappatoire.
- `--sync-graphdb`/`--cypher` (v4.0) sont entièrement optionnels (`pip install
  ladybug`) et n'affectent jamais le moteur JSON + BFS/Dijkstra existant — ce sont deux
  chantiers strictement parallèles, jamais un remplacement. `graph_db/` n'est pas
  resynchronisé automatiquement après un rebuild/`--update` : relancer `--sync-graphdb`
  pour que les requêtes Cypher voient les changements — depuis v4.1, `--cypher` le
  signale lui-même (`graph_db/ may be stale -- ...`) plutôt que de répondre en silence
  depuis un graphe périmé, mais ça reste un avertissement, pas une resynchronisation
  automatique. Une requête filtrant une relation à profondeur variable avec
  `all(x IN e WHERE ...)` échoue (erreur de type `RECURSIVE_REL`/`LIST`) — passer par
  une variable de chemin et filtrer `relationships(p)` à la place (voir
  `references/query_protocol.md`).
- tree-sitter couvre désormais JS/TS/Java (v4.0) et Go/Rust/C/C++/PHP (v4.2) — soit 8
  langages sur les 12 supportés ; Ruby/Swift/Kotlin restent en regex uniquement, par
  choix explicite (pas encore de demande justifiant le chantier), pas par limitation
  technique de l'approche elle-même.
- tree-sitter corrige la *structure* (ce qu'est une méthode, un import groupé) mais pas,
  à lui seul, la désambiguïsation d'un appel entre plusieurs candidats homonymes — c'est
  le rôle des deux tiers de résolution `RESOLVED` ajoutés en v4.3 (même fichier, import
  vérifié sur le système de fichiers), qui restent malgré tout un texte scanné et une
  résolution de chemin, pas une vraie résolution de portée/type : un nom masqué par une
  définition imbriquée, par exemple, n'est pas modélisé. Voir `confidence` dans
  `query_protocol.md` pour le détail complet des deux tiers et de l'ancien barème
  `INFERRED` qui reste le filet de sécurité partout ailleurs.
- Le clustering Leiden (v4.4) est optionnel et retombe silencieusement sur l'heuristique
  par dossier en dessous de 5 edges `calls`/`inherits` réels (pas assez de signal pour
  qu'un clustering veuille dire quoi que ce soit) ; `graph.json` ne porte aucune trace
  de quel algorithme a produit `communities` — un nom de communauté avec un suffixe
  `"(+N more dirs)"` est l'indice que c'est Leiden qui a tourné.
- `--diff`/`--snapshot` (v4.4) comparent par id de nœud, et cet id intègre le numéro de
  ligne du symbole — un symbole qui a simplement changé de ligne (sans changer lui-même)
  apparaît en `removed`+`added`, jamais en `changed`. `--diff` sans nom ne voit jamais
  que l'état d'avant le tout dernier build ; utiliser `--snapshot <nom>` pour un point de
  comparaison qui survit à plusieurs builds.
- Le détecteur générique par entropie du scanner de secrets (v4.4) reste un heuristique,
  pas un scanner fiable : un long littéral à haute entropie mais innocent (un blob
  encodé, un fixture de test) peut occasionnellement être rédacté à tort, et un vrai
  secret dont la valeur ressemble à du texte naturel (espaces, ponctuation) peut lui
  échapper — voir `SKILL.md` § "What changed in v4.4" pour le détail du calibrage.

## Tests

Suite pytest dev-only dans `tests/` (n'affecte en rien l'usage normal du skill) :

```bash
pip install -r tests/requirements-dev.txt
cd codegraph-explorer-skill
python -m pytest tests/ -v
```

Couvre le shrink-guard, le cache incrémental/dédup, le `.gitignore`/`.codegraphignore`
imbriqué (v4.1), les 3 bugs tree-sitter réels trouvés en construisant v4.0 (comme
régressions figées, pas comme relecture manuelle), le fallback tree-sitter→regex sur un
fichier syntaxiquement cassé, le piège Cypher `all()`/`relationships(p)`,
l'avertissement de fraîcheur de `graph_db/` (v4.1) et la correction du crash de
resync (v4.1), les commandes de requête (`--explain`/`--callers`/`--find-path`/
`--trace-entrypoints`/`--impact`), l'export Obsidian, et l'extraction tree-sitter
Go/Rust/C/C++/PHP (v4.2, `tests/test_tree_sitter_extended_languages.py` — imports
groupés Go, `impl Trait for Type` → `inherits` Rust, signatures multi-lignes C,
héritage C++, les 4 variantes `require`/`include` PHP, entre autres), et la résolution
d'appels `RESOLVED` (v4.3, `tests/test_calls_resolution.py` — même fichier, import
Python/JS-TS résolu sur le vrai système de fichiers à travers une extension différente,
un spécificateur de paquet nu type `require("lodash")` confirmé jamais traité comme un
chemin résolvable, et le barème `INFERRED` pré-v4.2 confirmé inchangé quand rien ne se
résout), et Phase 5 (v4.4) : le clustering Leiden vs l'heuristique par dossier et le
repli sous 5 edges (`tests/test_community_leiden.py`), le diff/snapshot de graphe y
compris la limite documentée du symbole déplacé verrouillée comme régression
(`tests/test_graph_diff.py`), et les nouveaux formats de secrets redigés plus les
non-régressions sur des valeurs bénignes réalistes (extension de `TestRedact` dans
`tests/test_unit_helpers.py`), et — v4.5 — le fait que `graph.html` reste sans
dépendance externe (`tests/test_html_offline.py` : aucun `<script src>`, aucun CDN,
aucun appel D3, verrouillé comme régression), la résolution `calls` par AST JS/TS
(`tests/test_calls_ast.py` : `if (`/`while (`/`catch (` jamais comptés comme appels,
`this.m()` → méthode de la même classe, appel dans un callback crédité à la méthode
englobante, appel nu préférant la fonction libre, `new X()` → classe, import résolu),
la clé de cache par hash de contenu (`tests/test_incremental_cache.py` : `mtime` bougé
+ contenu identique → servi du cache), le `--impact` prod/test
(`tests/test_impact_tests.py` : `metadata.role` sur les nœuds `file`, `tests_to_run`,
test atteint transitivement), et les edges `file → file`
(`tests/test_file_deps.py` : agrégation pondérée, absence dans `edges`, requête par
fichier et par symbole, liste projet triée par poids). Les tests qui dépendent de `tree-sitter`/`ladybug`/
`python-igraph`+`leidenalg` se sautent proprement (`pytest.skip`) si le paquet
correspondant n'est pas installé, plutôt que d'échouer.

## License

MIT
