# Demo script: real-data dashboard

A walk through every page and section of the dashboard, in presenting order. About 8 minutes.
**DO** lines are what you click; **SAY** lines are what you say. Every number below was
produced by a command in this repository on 2026-11-12 (the backtest report
`ml/reports/crp_chla_demo/2026-11-12/` and the `make demo-real` run).

## Before you start

1. Run `make demo-real` (about 1 minute). It loads the data, trains the models and starts the
   dashboard.
2. Open http://localhost:8501.
3. In the sidebar, switch off "Auto-refresh" so pages do not redraw while you talk.

## Opening (30 seconds)

**SAY:** "This is a digital twin for cyanobacteria bloom risk on the Rio Grande. Data comes in,
it is stored with a quality flag, models estimate bloom risk, and the dashboard shows the
result. Our sensors have not arrived, so today the data is real lab data: grab samples from the
IBWC Clean Rivers Program, about one sample per station per month. A bloom here means
chlorophyll-a at or above 10 micrograms per liter."

**SAY:** "We loaded 11,924 readings from 25 stations into the database through the same entry
point the sensor nodes will use."

## Page 1: Overview (45 seconds)

**DO:** Click **Overview**.

### Node cards

**SAY:** "Each card is one planned sensor node: the intake, the effluent input, the downstream
node and the lab bench. They say 'never seen' because the hardware is not installed yet. When a
node reports, its card shows the last time we heard from it, how many readings in the last hour
were good, suspect or bad, and its latest values."

**SAY:** "The line under the cards says 25 more nodes hold rows loaded from files. Those are
the lab stations, which are on the next pages."

### Active alerts

**SAY:** "The alert engine is the next piece we build. It will turn a model's bloom probability
into an advisory. It is not built yet, and the page says so."

### Champion model

**SAY:** "This is the model currently registered as the one in use: a random forest trained on
458 labeled samples up to May 2023."

## Page 2: Live signals (1 minute)

**DO:** Click **Live signals**. In the sidebar set **Time range** to **custom**, start
`2023-01-01`, end today. Pick node `crp-13202`. Add the channel `chlorophyll_a`.

### Pickers

**SAY:** "This page shows the stored readings themselves. I pick stations and channels, and the
time range in the sidebar."

### Chart

**SAY:** "Lines are measurements: here water temperature and pH. Diamonds are lab results: here
chlorophyll-a. Quality is drawn on the chart: a good reading is part of the line, a suspect
one is a hollow orange marker, a bad one is a red x. Only good readings are used to train
models."

### Table and download

**SAY:** "Below the chart is the same data as a table, with the quality flag and the source of
each row, and a button to download it."

## Page 3: Bloom timeline (3.5 minutes)

**DO:** Click **Bloom timeline**. Set **Station** to `crp-13202`.

### Pickers and the blue note

**SAY:** "The task is 'predict whether a sample is a bloom'. I pick a station. The blue note
reminds us this is real lab data and that station names and locations are still placeholders."

### When is the next bloom likely?

**SAY:** "This answers 'when should I expect the next bloom here?' For this station the next
likely window starts in about 6 months, May through August 2027."

**SAY:** "Each bar is a calendar month. Its height is the share of past samples at this station
around that month that were blooms; the numbers above each bar are blooms out of samples. Red
bars are months where at least half of past samples were blooms. That 50% level is a setting we
chose, not a standard."

**SAY:** "To be clear about what this is: it is a seasonal outlook from this station's history.
It is not driven by live sensor readings. A new lab result changes it; a new sensor reading
does not."

### All stations: next likely bloom window

**SAY:** "The same outlook for every station, soonest first. Seven of the 14 stations with
recent data have a likely window in the next 12 months; the rest never reach 50% in any month
of their history."

### What every model says about the latest sample

**SAY:** "This is the model side. For the most recent sample at this station, each model gives
a bloom probability and an estimated chlorophyll level with a 90% range. The cards show the
top-ranked model; the table shows all of them, plus two simple rules for comparison:
'persistence', which repeats the previous lab result, and 'station climatology', which is the
seasonal history from the chart above."

**SAY:** "These are estimates for the day of the sample, from that visit's water temperature,
pH, river flow, air temperature, the season and the station. Note the sample date: this is as
current as the newest lab sample."

### Which model ranks first?

**SAY:** "We scored every model on samples it had never seen. Random forest ranks first among
the models, 0.683 out of 1. But the simple 'station plus time of year' rule scores 0.684. They
are level, so we do not claim the model wins. The honest finding is that, with monthly lab
data, where and when you sample explains most of what the models learn."

**SAY (if asked what the columns mean):** "PR-AUC measures how well a model puts real blooms at
the top of its list; blind guessing scores about 0.2 here. Walk-forward tests one year at a
time from 2018 to 2023. Hold-out is the most recent 20% of sample dates, scored once."

### All models against the lab labels

**SAY:** "Every stored prediction for this station over the period no model was trained on. The
black diamonds are the actual lab results and the red dashed line is the bloom level. The thick
line is random forest; grey lines are the two simple rules. The top panel is the chlorophyll
estimate; the bottom panel is the bloom probability."

**DO:** Click a legend entry to hide a line, then click again to bring it back.

### Try it: simulate a sensor reading

**DO:** Click **Simulate a reading** two or three times.

**SAY:** "This is what happens when a new reading arrives. The button invents one random
reading for this station, inside the range of its past readings. The table shows the invented
values. The chart shows each model's bloom probability for it, next to the latest real sample
in grey."

**SAY:** "It is labeled simulated, nothing is stored, and it only shows how the models react to
their inputs. When the real sensors are connected, this same step runs on real readings."

## Page 4: Model benchmarks (1.5 minutes)

**DO:** Click **Model benchmarks**. Keep task `crp_chla_demo`, target "bloom event",
evaluation "walk-forward".

### Pickers

**SAY:** "This is the full scoreboard. I can switch the task, switch between predicting a bloom
yes-or-no and predicting the concentration, and switch between the two tests."

### Number cards

**SAY:** "This test used 199 samples on 92 sample dates, 40 of them blooms."

### Bar chart

**SAY:** "Grey bars are the simple rules, colored bars are trained models, longer is better.
The whiskers are the 90% uncertainty range, and the dotted line is what blind guessing would
score. The tree models and the station climatology rule overlap. Logistic regression is clearly
below them. Persistence, repeating the last lab result, is the weakest at 0.349, because a
month is a long time between samples."

### Table and verdicts

**SAY:** "The table gives the exact numbers and a verdict against each simple rule. We only
write 'beats' when the data can show the gap is real; otherwise it says 'no demonstrable
difference'. That rule is built into the software, so the dashboard cannot overstate a result."

### Expanders

**SAY:** "These explain what each simple rule means, list every verdict sentence from the
report, and show the report's own warnings, for example years that had no blooms to test on."

**DO (optional):** Switch **Evaluation** to "final hold-out".

**SAY:** "On the most recent data, held back until the end: random forest 0.787, station
climatology 0.781. The same conclusion."

## Closing (30 seconds)

**SAY:** "What works today: real data stored with quality flags, five models trained and
compared honestly against simple rules, every prediction stored, a seasonal bloom outlook for
every station, and a dashboard that shows all of it."

**SAY:** "What comes next: scoring stored predictions automatically when new lab results
arrive, turning the bloom probability into an alert, and connecting the sensor nodes."

## Questions you may get

- **"Is the countdown a forecast from your sensors?"** No. It is a seasonal outlook from each
  station's lab history. A sensor-driven forecast needs a model trained to predict the next
  sample, tested the same way; that is not built.
- **"So the models add nothing?"** With monthly lab data, no model shows a demonstrable gain
  over "station plus time of year". The models do beat persistence and the pooled seasonal
  rule. Continuous sensor data may change this, and the software will report it either way.
- **"Why is the latest sample months old?"** The lab program publishes with a delay; the newest
  sample in our files is from 2026-05-18.
- **"Where are these stations?"** Their names and locations are placeholders in our
  configuration until we confirm them.
- **"Does the system treat the river?"** No. Its output is an advisory.
