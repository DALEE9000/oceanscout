# Taste

Edit this file freely. The whole thing is handed to the model verbatim for every digest and every
`ask`. Two sections are also parsed mechanically: bullets under **Track** are a ranking boost when
choosing what the digest and the Firehose show first; bullets under **Ignore** are case-insensitive
substrings that remove an item before the model ever sees it.

## Who I am

Columbia / Lamont ocean researcher. Core work: inferring sea surface velocity (SSV) fields from
sparse and coastal data: SWOT wide-swath altimetry with machine learning, validated against HF
radar (HFR) surface currents and drifters. I want to know the state of the art, who is doing it,
and where the unoccupied niches are. I also want broad awareness of physical oceanography beyond
my own problem.

## Track

- sea surface velocity
- surface current reconstruction
- SWOT altimetry
- submesoscale
- balanced unbalanced separation
- internal tide
- geostrophy breakdown
- HF radar
- drifter
- gap filling
- 4DVarNet
- neural mapping
- generative model ocean
- uncertainty quantification
- sim-to-real
- LLC4320
- ocean state estimation
- mesoscale eddies
- ocean mixing
- boundary current

## Ignore

- coral
- fisheries
- aquaculture
- microplastic
- plankton
- sediment transport
- paleoclimate
- marine mammal

## Notes

Directories of your own markdown notes, relative to this folder. Indexed and shown to the model as `[my notes]`.

- ../ml-ssv-literature-review

## Venues and people to watch

- JPO, JTECH, JAMES, GRL, Ocean Science, Ocean Modelling, AIES, Remote Sensing of Environment
- SWOT science team output (Tchonang, Ubelmann, Ballarotta, Le Guillou, Fablet's 4DVarNet group)
- HFR and drifter community (Kirincich, Roarty, Elipot, Lumpkin)

## What a must-read looks like

- A new method validated against independent observations, not only against a simulation.
- Anything that quantifies error, uncertainty, or failure modes (where does geostrophy break, where does ML hallucinate).
- New datasets, missions, or open code I could use.
- Results that contradict a consensus or close a gap my notes call open.

## What is low value

- Regional case studies with no methodological novelty.
- Incremental ML papers that only swap the architecture on the same benchmark.
- Pure climate-model intercomparisons unless they say something about surface currents.
