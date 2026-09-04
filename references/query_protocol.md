# Query Protocol — JSON-First (v4.4)

## Principe
Toutes les requêtes sont résolues à partir du **graphe**, jamais des sources — mais
« à partir du graphe » ne veut pas dire « Claude charge `graph.json` en entier ». Sur un
vrai projet ce fichier peut peser des dizaines de Mo (mesuré : 31,7 Mo pour ~10 500
nœuds sur 206 fichiers) ; le charger dans le contexte pour répondre à une question
ciblée coûterait largement plus que l'exploration classique que ce skill est censé
remplacer. Les commandes de requête du script (`--explain`, `--callers`, `--find-path`,
`--trace-entrypoints`) existent précisément pour ça : elles font la recherche/traversée
dans le processus Python et n'impriment que le résultat.

## Workflow de réponse

1. **Route** : question ciblée (un symbole, une relation entre deux symboles) →
   commande de requête du script ; question large (architecture globale) →
   `GRAPH_REPORT.md` ; sinon, en dernier recours, `graph.json` complet.
2. **Explain** : `python codegraph_builder.py <path> --explain <symbole>` — métadonnées
   + edges entrants/sortants avec confidence, ou une liste de candidats si le nom est
   ambigu (ré-exécuter avec l'id exact affiché).
3. **Callers** : `python codegraph_builder.py <path> --callers <symbole>` — uniquement
   qui appelle/hérite de ce symbole, trié par confidence.
4. **Path** : `python codegraph_builder.py <path> --find-path <A> <B>` — plus court
   chemin pondéré (les edges `contains` sont pénalisées par rapport à `calls`/
   `inherits`, pour préférer une vraie relation de code à « ils sont dans le même
   fichier »).
5. **Entrypoints** : `python codegraph_builder.py <path> --trace-entrypoints <symbole>`
   — remonte les edges `calls`/`inherits` en sens inverse jusqu'au(x) `entrypoint` le(s)
   plus proche(s), en un seul appel plutôt que plusieurs `--callers` manuels.
6. **Impact** : `python codegraph_builder.py <path> --impact <symbole>` (v3.4) — la
   fermeture transitive complète (pas juste un saut) de tout ce qui dépend du symbole
   via `calls`/`inherits`, avec les entrypoints impactés signalés séparément. À utiliser
   pour "si je change/casse X, qu'est-ce qui est affecté", par opposition à `--callers`
   (un seul saut) ou `--trace-entrypoints` (s'arrête au premier entrypoint atteint).
6bis. **Cypher** (v4.0, optionnel) : `python codegraph_builder.py <path> --sync-graphdb`
   une fois (exporte `graph.json` vers `.codegraph/graph_db/`, à relancer après tout
   rebuild/`--update` dont les requêtes Cypher doivent voir les changements — ce n'est
   pas automatique), puis `python codegraph_builder.py <path> --cypher "<requête>"`.
   C'est une couche **parallèle** au moteur JSON + BFS/Dijkstra ci-dessus, jamais un
   remplacement — voir la section dédiée plus bas pour le schéma exact et quand s'en
   servir plutôt que les commandes 2 à 6.
7. **Weigh** : pondérer les edges `calls` par leur `confidence` (voir plus bas) en
   synthétisant la réponse.
8. Ajouter `--json` à n'importe laquelle de ces commandes si la sortie doit être
   post-traitée plutôt que simplement relayée à l'utilisateur.
9. **Cypher** (`--cypher`, v4.0, optionnel — voir plus bas) : uniquement pour ce
   qu'aucune commande ci-dessus ne couvre déjà bien (motif multi-sauts, agrégation,
   filtre ad-hoc sur beaucoup de nœuds à la fois). Ne jamais l'utiliser en remplacement
   de `--explain`/`--callers`/`--find-path`/`--trace-entrypoints`/`--impact` pour les
   questions qu'elles couvrent déjà — ces commandes ne nécessitent aucune dépendance et
   leur sortie est déjà formatée pour la question posée, contrairement à une table
   brute de résultats Cypher.

## Règles

### Règle d'or
> Si la question concerne l'architecture, les dépendances, ou le fonctionnement du code
> → **le graphe d'abord**, via les commandes de requête ci-dessus plutôt qu'en chargeant
> `graph.json` en entier. Ne jamais ouvrir de fichier source sauf si le JSON indique
> explicitement `[INCOMPLETE]` (le script actuel n'écrit jamais ce marqueur — en
> pratique, donc : jamais).

### Citation
- Toujours citer les edges avec leur tag : `[EXTRACTED]`, `[INFERRED]`, ou `[RESOLVED]`
  (v4.2, voir plus bas)
- Indiquer le chemin et la ligne du nœud quand pertinent

### Pondération par confiance (edges `calls`)

Un edge `calls` est fondamentalement une heuristique par nom, pas une résolution de
portée réelle : le script cherche, dans le corps de chaque fonction, les identifiants
suivis de `(` qui correspondent au nom d'un autre symbole. Deux fonctions sans rapport
portant le même nom dans deux fichiers différents peuvent donc se retrouver reliées à
tort. Depuis v4.2, deux niveaux de résolution existent, dans cet ordre de priorité pour
chaque appel :

**1. `tag: "RESOLVED"`** — le script a une preuve concrète, pas seulement un décompte de
candidats, que l'appel vise un symbole précis :
- `metadata.resolved_by: "same_file"` (confidence `0.97`) : un seul des candidats
  portant ce nom est défini **dans le même fichier** que l'appelant — n'importe quel
  langage, aucune analyse d'import nécessaire.
- `metadata.resolved_by: "import"` (confidence `0.93`) : l'import de l'appelant se
  résout, sur le vrai système de fichiers (pas par ressemblance de nom), vers exactement
  un des fichiers candidats. Portée à ce jour : imports relatifs JS/TS/JSX/TSX (`./foo`,
  `../bar/baz`) et chemins de module Python en pointillés (`import pkg.sub.mod` /
  `from pkg.sub import x`), résolus depuis la racine du projet. Go/Rust/Java/PHP/C/C++
  n'ont pas cette résolution vérifiée (leurs chemins d'import ont besoin de métadonnées
  d'outillage — `go.mod`, un classpath, `composer.json`, un chemin d'include — que le
  script ne parse pas) ; ils restent sur le signal plus faible ci-dessous.

Ni l'un ni l'autre n'atteint `1.0` : c'est toujours un scan texte du corps de la
fonction, pas une vraie résolution de portée (une définition imbriquée qui masquerait
le symbole global, par exemple, n'est pas modélisée). Mais un edge `RESOLVED` peut être
présenté avec un niveau de confiance nettement supérieur à un edge `INFERRED` — c'est
la différence entre une preuve et une coïncidence de nom.

**2. `tag: "INFERRED"`** — aucune des deux résolutions ci-dessus n'a permis de trancher
(ou en langage sans résolution d'import vérifiée) ; le champ `confidence` reflète alors
le nombre de candidats possibles portant ce nom (dans le sous-ensemble déjà réduit par
une résolution d'import partielle, quand elle existe — voir plus bas) :

| candidats portant ce nom | confidence de base | interprétation |
|---|---|---|
| 1 (nom unique parmi les candidats considérés) | 0.85 | quasi certain |
| 2–3 | 0.55 | plausible, à vérifier si ça compte |
| 4–8 | 0.35 | coïncidence probable |
| 9+ | 0.2 | quasiment toujours une coïncidence de nom (`get`, `close`, `__init__`...) |

Cette base reçoit un bonus `+0.25` (plafonné à `0.95`, depuis v3.3) quand le fichier de
l'appelant importe explicitement quelque chose dont le chemin *ressemble* au fichier du
candidat précis (comparaison par nom de fichier, pas par chemin résolu — c'est le signal
plus faible qui reste le seul disponible pour Go/Rust/Java/PHP/C/C++, ou pour un import
JS/TS/Python que la résolution vérifiée n'a pas réussi à faire correspondre à un fichier
réel). Ce bonus est un indice supplémentaire, pas un changement d'échelle : un nom
partagé par neuf autres symboles ne monte qu'à ~0.45 même avec un import correspondant.
Séparément, quand la résolution d'import vérifiée (tier `RESOLVED` ci-dessus) réduit
l'ensemble de candidats sans l'amener à exactement un seul (par exemple deux fichiers
tous deux réellement importés définissent chacun un symbole du même nom), c'est ce
sous-ensemble réduit — pas l'ensemble complet du projet — qui sert de base au tableau
ci-dessus ; un appel qui aurait été « 9+ candidats, 0.2 » avant v4.2 peut ainsi devenir
« 2 candidats réellement importés, 0.55 » sans jamais atteindre le tier `RESOLVED`. En
pratique : pour une réponse du type "qu'est-ce qui appelle X", mets en avant les edges
`RESOLVED` en premier, puis les `INFERRED` avec `confidence >= 0.55`, sauf si
l'utilisateur demande explicitement une liste exhaustive — et dans ce cas, dis
clairement que les entrées `INFERRED` à faible confidence sont des coïncidences de nom
probables, pas des appels vérifiés. Sur un vrai projet, une part significative des edges
`calls` reste `INFERRED` à confidence moyenne ou basse (noms courts et courants,
langages sans résolution d'import vérifiée) même après v4.2 — c'est attendu, pas un
signe que le graphe est cassé.

### Types de requêtes

#### "Explain X" / "What does X do"
- `--explain X` : le script fait le lookup (id exact, nom exact, puis substring en
  dernier recours) et imprime type/path/line/metadata + edges entrants/sortants triés
  par confidence + community/degree. Si `X` est ambigu, le script liste les candidats
  avec leurs ids exacts plutôt que d'en choisir un au hasard — relayer cette liste à
  l'utilisateur ou ré-exécuter avec l'id précis, ne pas deviner.

#### "What connects A to B?" / "How does A relate to B?"
- `--find-path A B` : plus court chemin pondéré (edges `calls`/`inherits` préférées à
  `contains`). Détailler chaque hop tel qu'imprimé :
  `A --[calls/INFERRED, conf=0.85]→ C --[imports/EXTRACTED]→ B`.
- Si le chemin retourné ne contient que des edges à faible confidence, le dire
  explicitement plutôt que de le présenter comme une relation établie.

#### "What depends on X?" / "What calls X?"
- `--callers X` : tous les nodes avec un edge `calls`/`inherits` **vers** X, déjà triés
  par confidence décroissante. Ne pas re-trier par « nombre de chemins » — cette
  commande ne calcule pas ça, ne pas l'inventer.

#### "Find entry points to X"
- `--trace-entrypoints X` (v3.3) : remonte les edges `calls`/`inherits` en sens inverse
  (jamais `contains` — un voisin de fichier n'est pas un appelant) jusqu'aux
  `entrypoint` les plus proches, en un seul appel. Imprime jusqu'à 5 chemins distincts
  avec chaque hop détaillé, comme `--find-path`. Avant v3.3 il fallait ré-exécuter
  `--callers` à la main sur chaque appelant trouvé jusqu'à tomber sur un `entrypoint` —
  cette commande fait exactement ça côté script.
- Si rien n'est trouvé (message explicite plutôt qu'une liste vide silencieuse), le
  symbole est soit du code mort/jamais atteint depuis un entrypoint détecté, soit
  atteint via un pattern d'appel que la résolution heuristique de `calls` ne capte pas
  — le dire, ne pas conclure à tort que le symbole est inatteignable dans les faits.

#### "What would break if I changed X?" / "What's the blast radius of X?"
- `--impact X` (v3.4) : fermeture transitive complète (BFS, jusqu'à 15 sauts par
  défaut) de tout ce qui appelle/hérite de X directement ou indirectement, groupé par
  profondeur, avec la liste des entrypoints impactés à part. Contrairement à
  `--trace-entrypoints`, ne s'arrête pas au premier entrypoint — il continue la
  traversée et rapporte l'ensemble complet. Comme pour `--callers`, ne pas présenter un
  nœud atteint uniquement via des edges `calls` à faible confidence comme un impact
  certain — le mentionner avec sa confidence, surtout à une profondeur élevée où les
  incertitudes s'accumulent.

#### Requêtes multi-sauts / agrégations / filtres ad-hoc — Cypher (v4.0)
- `--cypher "<requête>"` (nécessite `--sync-graphdb` au préalable et `pip install
  ladybug`) : escape hatch pour ce qu'aucune commande dédiée ne couvre déjà bien —
  motif multi-sauts arbitraire, agrégation (`count`, `collect`, ...), filtre combinant
  plusieurs propriétés à la fois sur un grand nombre de nœuds. Schéma :
  `Symbol(id, name, ntype, path, line_start, line_end, community, degree, meta)` et
  `Edge(FROM Symbol TO Symbol, etype, tag, confidence, meta)` — `ntype`/`etype`
  reprennent exactement le vocabulaire `type`/`tag` de `graph.json` (voir
  `references/graph_schema.md` pour le détail), `meta` est le champ `metadata` encodé en
  JSON-string.
- Ne pas l'utiliser pour ce que `--explain`/`--callers`/`--find-path`/
  `--trace-entrypoints`/`--impact` couvrent déjà — ces commandes ne demandent aucune
  dépendance et produisent une sortie déjà formatée pour la question posée ; une table
  de résultats Cypher bruts pour "qu'est-ce qui appelle X" serait un pas en arrière, pas
  un progrès.
- **Piège à connaître** : filtrer une relation à profondeur variable
  (`[e:Edge*1..2]`) avec `all(x IN e WHERE ...)` échoue — `e` se lie comme un
  `RECURSIVE_REL`, pas la `LIST` attendue par `all()`. Nommer le chemin et filtrer
  `relationships(p)` à la place :
  ```cypher
  MATCH p = (a:Symbol {name:'AuthService'})-[:Edge*1..2]->(b:Symbol)
  WHERE all(x IN relationships(p) WHERE x.etype IN ['calls','inherits'])
  RETURN DISTINCT b.name, b.ntype
  ```
- Si `ladybug` n'est pas installé, ou si `.codegraph/graph_db/` n'existe pas encore, le
  script imprime un message clair (commande d'installation, ou "lancer `--sync-graphdb`
  d'abord") plutôt que de planter — relayer ce message tel quel plutôt que d'improviser
  une explication.
- Comme pour `--impact`/`--callers`, ne jamais présenter un résultat obtenu via un edge
  `calls` à faible `confidence` comme une relation établie — le champ `confidence` est
  disponible dans `Edge.confidence`, à inclure dans la requête (`RETURN`) et à
  redescendre dans la réponse si le résultat en dépend.
- **Fraîcheur (v4.1)** : `graph_db/` n'est pas resynchronisé automatiquement après un
  rebuild/`--update`. Chaque `--cypher` compare désormais le `graph.json` utilisé au
  dernier `--sync-graphdb` contre le `graph.json` actuel et imprime `graph_db/ may be
  stale -- ...` (une clé `warning` en `--json`) si le second a été reconstruit depuis —
  relayer cet avertissement à l'utilisateur plutôt que de présenter le résultat comme à
  jour, et suggérer de relancer `--sync-graphdb`. C'est un avertissement, pas un blocage
  : la requête s'exécute et retourne son résultat quand même.

#### "What changed since the last build?" / "What changed since I started this?"
- `--diff` (sans nom, v4.4) : compare le `graph.json` actuel à l'état d'avant le
  dernier build/`--update` (`.codegraph/.graph_prev.json`, roté automatiquement à
  chaque `save()` — fonctionne sans aucune préparation dès qu'au moins deux builds ont
  eu lieu). `--diff <nom>` compare à un snapshot nommé pris plus tôt avec
  `--snapshot <nom>` — reste comparable même après plusieurs builds intermédiaires,
  contrairement à `--diff` sans nom qui ne voit jamais que le build immédiatement
  précédent.
- Le résultat liste nœuds/edges `added`/`removed`/`changed` (`--json` pour la forme
  structurée — voir `references/graph_schema.md` pour le détail exact des clés).
  `changed` pour un edge `calls` reflète un changement de `tag`/`confidence` (par
  exemple une résolution `INFERRED` à 0.55 devenue `RESOLVED` à 0.97 une fois le code
  assez modifié pour que la résolution v4.3 s'applique) — signaler ce genre de
  changement explicitement plutôt que le noyer dans une liste brute.
- **Limite à toujours mentionner** : les ids de nœuds intègrent leur propre numéro de
  ligne. Un symbole qui a simplement *bougé* (une ligne ajoutée au-dessus, rien changé
  dans le symbole lui-même) apparaît comme un `removed` + un `added` à une nouvelle
  ligne, jamais comme un seul `changed` — ne pas présenter une rafale d'ajouts/
  suppressions du même nom comme forcément une vraie réécriture sans vérifier s'il
  s'agit juste d'un déplacement.
- Si aucun état de comparaison n'existe (premier build, ou nom de snapshot inconnu), le
  script imprime un message clair plutôt que de planter silencieusement — le relayer
  tel quel (suggérer `--snapshot <nom>` si un nom inconnu a été demandé).

#### Architecture globale
- Utiliser `communities` pour décrire les modules (leur `description` est générée
  heuristiquement — le dire si tu la cites telle quelle). Depuis v4.4, `communities`
  peut venir d'un vrai clustering Leiden sur les edges `calls`/`inherits` plutôt que du
  regroupement par dossier (selon `--community-algo` au moment du build) — un nom de
  communauté qui mentionne plusieurs dossiers (`"services (+2 more dirs)"`) est le
  signe que c'est le cas ; dans ce cas, présenter la communauté comme un regroupement
  fonctionnel réel (les membres sont reliés par de vrais appels), pas comme un
  regroupement par emplacement de fichier.
- Utiliser `god_nodes` pour identifier les composants centraux (les nœuds `file` en
  sont exclus par construction — voir `graph_schema.md`)
- Utiliser `entrypoints` pour décrire les points d'entrée
