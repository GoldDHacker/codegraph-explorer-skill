# Auto-Build Protocol — Script-First Edition (v4.6)

## Principe
Le graphe est construit par un **script local**, pas par Claude. Claude est
l'orchestrateur, le script est le worker.

## Règles d'activation

### Règle 1 : First Encounter → Run
**Quand** : Claude ouvre un répertoire contenant du code.
**Action** :
1. Vérifier `.codegraph/graph.json`. Si présent et frais (voir Règle 3) → skip.
2. Si absent → exécuter `python codegraph_builder.py <racine_du_projet>` (script complet
   fourni dans `scripts/`, aucune adaptation par projet requise — il détecte les
   langages par extension).
3. Le script produit `.codegraph/graph.json`, `.codegraph/GRAPH_REPORT.md`,
   `.codegraph/graph.html`. `.codegraph/obsidian_vault/` n'est **pas** produit par
   défaut (voir Règle 5) — inutile de le générer tant que l'utilisateur ne le demande
   pas explicitement.
4. Notifier : "Graph built. {N} nodes, {M} edges." (reprendre les chiffres affichés par
   le script, pas des valeurs inventées).
5. Si le script refuse de continuer (exit code 1, message sur un nombre de fichiers qui
   s'est effondré par rapport à `.file_cache.json`) : c'est le shrink-guard (v3.4), pas
   un crash. Ne pas boucler dessus ni relancer avec `--force-rebuild` de sa propre
   initiative — relancer avec `--verbose` pour voir ce qui est ignoré et pourquoi (un
   chemin erroné, un pattern `.gitignore`/`.codegraphignore` trop large), et si la
   cause reste peu claire, demander à l'utilisateur avant de forcer.
6. Ce build utilise tree-sitter (v4.0) pour JS/TS/Java/Go/Rust/C/C++/PHP quand les
   paquets sont installés (`pip install "tree-sitter>=0.25,<0.26" tree-sitter-javascript
   tree-sitter-typescript tree-sitter-java tree-sitter-go tree-sitter-rust tree-sitter-c
   tree-sitter-cpp tree-sitter-php`), sans rien changer côté Claude : mêmes commandes,
   même format de sortie, méthodes de classe et résolution `calls` par AST en plus si
   présent. Si un build affiche `[!] tree-sitter grammar(s) installed but not
   loadable`, lancer `python codegraph_builder.py --doctor` et relayer la ligne `pip`
   qu'il propose à l'utilisateur — sinon, rien à faire ni à vérifier ici.

### Règle 2 : Query → Jamais le JSON en entier
**Quand** : L'utilisateur pose une question sur le code.
**Action** :
1. Vérifier `.codegraph/graph.json`.
2. Si absent → appliquer Règle 1.
3. Question ciblée sur un symbole ou une relation → lancer `--explain`/`--callers`/
   `--find-path`/`--trace-entrypoints` (le script fait la recherche/traversée, Claude ne
   lit que le résultat, quelques centaines de tokens quelle que soit la taille de
   `graph.json`).
4. Question large sur l'architecture globale → lire `GRAPH_REPORT.md` (quelques Ko), pas
   `graph.json`.
5. Charger `graph.json` en entier seulement si aucune des deux options ci-dessus ne
   couvre la question — sur un vrai projet ce fichier peut peser plusieurs dizaines de
   Mo (mesuré : 31,7 Mo sur 206 fichiers / ~10 500 nœuds), donc c'est le dernier recours,
   pas la règle par défaut.
6. Répondre en citant les nœuds et edges, avec la `confidence` pour les edges `calls`
   (voir `query_protocol.md`).
7. Jamais les fichiers sources pour répondre à une question d'architecture.

### Règle 2 bis : Question ouverte/interprétative → `--subgraph`, jamais une supposition (v4.6)
**Quand** : la question ne se réduit pas à une seule chose pré-calculée par la Règle 2 —
"est-ce que X est bien isolé", "pourquoi cette architecture tient", "quelle est la forme
de ce cluster", "détecte un problème dans cette zone".
**Action** :
1. `python codegraph_builder.py <racine_du_projet> --subgraph <symbole> --depth 2 --json`
   — voisinage borné (nœuds + edges, plafonné à 60 nœuds par défaut, `"truncated": true`
   si atteint) au lieu de deviner à partir de `--explain`/`--impact`, et surtout au lieu
   d'ouvrir des fichiers source.
2. Le script calcule déjà, sur ce voisinage : `cut_vertices`/`focus_is_cut_vertex`
   (points d'articulation, algorithme de Tarjan), `components_if_focus_removed`, et
   `cycles_through_focus` (cycles simples représentatifs). **Ne jamais** essayer de
   retrouver un cycle ou un point de coupure à l'œil en relisant la liste de nœuds/edges
   — c'est du calcul mécanique exact, pas de l'interprétation, et un LLM qui recompte des
   arêtes à la main se trompe régulièrement au-delà d'une poignée de nœuds. Ces champs
   sont scopés au voisinage extrait uniquement — pas une garantie sur tout le projet.
3. Les edges `calls` à faible confidence (< 0.5) sont exclus par défaut — ne pas
   redemander `--include-low-confidence` sans raison précise.
4. Claude apporte l'interprétation par-dessus ces faits calculés (voir
   `query_protocol.md` pour un exemple complet de synthèse).
5. Toujours préférer les commandes de la Règle 2 quand l'une d'elles répond déjà à la
   question — `--subgraph` coûte nettement plus cher (mesuré : ~38 Ko même après
   troncature à 60 nœuds, sur un nœud hub réel d'un projet de 807 nœuds) qu'un
   `--callers`/`--explain` à quelques centaines de tokens.

### Règle 3 : Stale Detection → Incremental Re-run
**Quand** : `generated_at` dans `graph.json` précède la date de modification du fichier
source le plus récent du projet.
**Action** : `python codegraph_builder.py --update <racine_du_projet>`.
C'est un vrai build incrémental : le script garde un cache par fichier
(`.codegraph/.file_cache.json`, clé = chemin + mtime) et ne reparse que ce qui a changé
— tout le reste (résolution des `calls`, communities, métriques) est recalculé en
mémoire à partir du cache fusionné, ce qui est peu coûteux et garantit qu'aucun rebuild
répété ne duplique de nœuds ou d'edges (propriété vérifiée : trois builds consécutifs
sur un projet inchangé produisent exactement le même graph.json, octet pour octet sur
les comptes de nœuds/edges).

### Règle 4 : Continuous Mode
**Quand** : Utilisateur demande "watch" ou "live".
**Action** : `python codegraph_builder.py --watch <racine_du_projet> &`
Le mode watch réutilise la même logique incrémentale que `--update` à chaque changement
détecté (avec `watchdog` si installé, sinon un polling 5s en fallback) — ce n'est ni un
rebuild complet à chaque frappe, ni un simple append sur l'état précédent. Si le
shrink-guard refuse un rebuild pendant le watch, le processus continue de tourner (il
ne crashe pas) — il retentera au prochain changement détecté.

### Règle 5 : Export Obsidian → À la demande uniquement
**Quand** : L'utilisateur demande explicitement à voir/exporter le graphe dans Obsidian
(jamais automatiquement, contrairement aux Règles 1/3/4).
**Action** : `python codegraph_builder.py --export-obsidian <racine_du_projet>` (v3.4).
Régénère `.codegraph/obsidian_vault/` depuis le `graph.json` existant uniquement — même
contrat que `--report` (rien n'est reparsé). Indiquer à l'utilisateur d'ouvrir ce
dossier comme un vault Obsidian, ou de le copier/symlinker dans un vault existant.

### Règle 6 : Sync graph_db / Cypher → À la demande uniquement
**Quand** : L'utilisateur pose une question multi-sauts/agrégation que
`--explain`/`--callers`/`--find-path`/`--trace-entrypoints`/`--impact` ne couvrent pas
bien, ou demande explicitement une requête Cypher (jamais automatiquement, même
contrat que la Règle 5).
**Action** (v4.0, nécessite `pip install ladybug`) :
1. `python codegraph_builder.py --sync-graphdb <racine_du_projet>` — exporte le
   `graph.json` existant vers `.codegraph/graph_db/`, même contrat que `--report`/
   `--export-obsidian` (rien n'est reparsé). Ce dossier n'est **pas** resynchronisé
   automatiquement après un rebuild/`--update` : relancer cette commande si le graphe a
   changé depuis le dernier `--sync-graphdb`.
2. `python codegraph_builder.py --cypher "<requête>" <racine_du_projet>` — exécute la
   requête et n'imprime que le résultat. Si `ladybug` n'est pas installé ou si
   `graph_db/` n'existe pas encore, le script le dit clairement plutôt que de planter —
   ne pas boucler dessus, relayer le message à l'utilisateur. Depuis v4.1, chaque appel
   compare aussi `graph_db/` au `graph.json` actuel et imprime `graph_db/ may be stale
   -- ...` s'il détecte que `graph.json` a été reconstruit depuis le dernier sync (une
   clé `warning` en sortie `--json`) — relayer cet avertissement à l'utilisateur plutôt
   que de présenter un résultat potentiellement périmé comme à jour ; ce n'est qu'un
   avertissement, la requête s'exécute quand même.
3. Cette couche est strictement additive au moteur JSON + BFS/Dijkstra des Règles 1-4 :
   ne jamais l'utiliser pour une question que `--explain`/`--callers`/`--find-path`/
   `--trace-entrypoints`/`--impact` couvrent déjà — voir `references/query_protocol.md`
   pour le détail (schéma générique `Symbol`/`Edge`, piège Cypher `all()` sur relation à
   profondeur variable).

## Anti-patterns STRICTEMENT INTERDITS
- ❌ Claude ouvre un fichier source pour en extraire des symboles
- ❌ Claude utilise regex dans son raisonnement pour parser du code
- ❌ Claude demande à l'utilisateur s'il veut construire le graphe
- ❌ Claude lit 20 fichiers pour répondre à "comment fonctionne X ?"
- ❌ Claude présente un edge `calls` à faible `confidence` comme un fait établi sans le
  signaler (voir `query_protocol.md`)
- ❌ Claude essaie de détecter un cycle ou un point de coupure à l'œil en lisant une
  sortie `--subgraph` au lieu d'utiliser les champs déjà calculés (v4.6)

## Patterns obligatoires
- ✅ Claude exécute le script, le script fait le travail — y compris pour répondre aux
  questions (`--explain`/`--callers`/`--find-path`/`--trace-entrypoints`), pas seulement
  pour construire le graphe
- ✅ Claude cite `[EXTRACTED]` vs `[INFERRED]`, et la `confidence` pour les edges `calls`
- ✅ Mise à jour incrémentale via `--update` ou le mode watch, jamais un rebuild complet
  systématique (`/graph build` sans arguments force un rebuild complet — à réserver aux
  cas listés dans `SKILL.md`)
