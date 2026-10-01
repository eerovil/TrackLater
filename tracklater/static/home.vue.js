var home = Vue.component("home", {
    template: `
    <div>
    <toolbar
        class="toolbar"
        v-on:fetchModule=fetchModule($event)
        v-on:exportEntry=updateEntry($event)
        v-on:setToolbarHeight=setToolbarHeight($event)
        v-on:populateLocal=populateLocal($event)
        v-bind:style="{ height: toolbarHeight }"
    ></toolbar>
    <div
        class="toolbar-separator"
        v-bind:style="{ height: toolbarSepHeight }"
    ></div>
    <div v-if="populate.active" class="populate-progress" style="
        position: fixed; left: 0; right: 0; bottom: 0; z-index: 1000;
        background: #1e1e2e; color: #fff; padding: 8px 14px;
        box-shadow: 0 -2px 8px rgba(0,0,0,0.3); font-size: 13px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:5px;">
            <span>Generating local entries with Codex GPT-5.6 Sol for {{ populate.label }}…<span v-if="populate.total"> (day {{ populate.done }}/{{ populate.total }})</span></span>
            <span style="display:flex; align-items:center; gap:10px;">
                <span>{{ fmtTime(populate.elapsedSec) }} / ~{{ fmtTime(populate.etaSec) }}</span>
                <button v-if="!populate.cancelling" @click="cancelPopulate"
                    style="background:#5a3a4e; color:#fff; border:none; border-radius:4px;
                           padding:3px 10px; cursor:pointer; font-size:12px;">Cancel</button>
                <span v-else style="color:#caa;">cancelling…</span>
            </span>
        </div>
        <div style="height:8px; background:#3a3a4e; border-radius:4px; overflow:hidden;">
            <div v-bind:style="{
                width: populate.progress + '%',
                height: '100%',
                background: populate.progress >= 100 ? '#4caf50' : '#7c6cff',
                transition: 'width 0.25s linear' }"></div>
        </div>
    </div>
    <daytimeline
      v-for="dateGroupData in entriesByDategroup"
      ref="daytimelines"
      :entries="dateGroupData.entries"
      :key="dateGroupData.dateGroup"
      @addEntry="updateEntry"
      @updateEntry="updateEntry"
      @deleteEntry="deleteEntry"
      @codexEntry="codexEntry"
      @fillDay="populateDay"
    ></daytimeline>
    </div>
    `,
    data() {
        return {
            toolbarHeight: '110px',
            toolbarSepHeight: '110px',
            populate: {
                active: false,
                progress: 0,
                elapsedSec: 0,
                etaSec: 0,
                timer: null,
                done: 0,
                total: 0,
                dayStartedAt: 0,
                controller: null,
                cancelling: false,
                label: 'this week',
            },
        }
    },
    computed: {
        modules() {
            return this.$store.state.modules;
        },
        entriesByDategroup() {
            // Return an array of objects, containing {dateGroup, entries}
            let keys = new Set()
            let ret;
            try {
            for (module_name in this.modules) {
                let entries = this.modules[module_name].entries || []
                for (let i=0; i<entries.length; i++) {
                    keys.add(entries[i].date_group)
                }
            }
            // Sort keys to make latest dategroup first
            ret = []
            for (dateGroup of Array.from(keys.values()).sort((a, b) => a > b ? -1 : 1)) {
                ret.push({dateGroup, entries: []});
            }
            for (module_name in this.modules) {
                let entries = this.modules[module_name].entries || []
                for (let i=0; i<entries.length; i++) {
                    entries[i].module = module_name
                    const index = ret.findIndex((item) => item.dateGroup === entries[i].date_group);
                    ret[index].entries.push(entries[i]);
                }
            }
            } catch (e) {
                console.log(e)
            }
            return ret;
        },
        debounceUpdateWeek() {
            return _.debounce(this.updateWeek, 500)
        }
    },
    methods: {
        fetchModule(module_name, parse) {
            if (parse == undefined) {
                parse = 1;
            }
            console.log(`Fetching ${module_name}`)
            this.$store.commit('setLoading', {module_name, loading: true});

            axios.get("fetchdata", {params: {
                    parse: parse,
                    keys: [module_name],
                    from: this.$store.getters.getFrom,
                    to: this.$store.getters.getTo,
                }}).then(response => {
                console.log(response)
                this.$store.commit('updateModules', response.data);
                this.$store.commit('setLoading', {module_name, loading: false});
            }).catch(() => {
                this.$store.commit('setLoading', {module_name, loading: false});
            })
        },
        parseTime(time) {
            if (typeof time === "string") {
                return new Date(time)
            }
            return time
        },
        updateEntry(entry) {
            this.$store.commit('setLoading', {module_name: 'updateentry', loading: true});
            updated_entries = this.$store.state.modules[entry.module].entries.filter((_entry) => _entry.id !== entry.id);
            let placeholderid;
            if (!entry.id) {
                placeholderid = "placeholderid" + Math.random();
            }
            updated_entries.push({
                id: entry.id || placeholderid,
                start_time: this.parseTime(entry.start_time),
                end_time: this.parseTime(entry.end_time),
                title: entry.title || "Placeholder",
                module: entry.module,
                date_group: this.parseTime(entry.start_time).toISOString().split('T')[0],
            })
            this.$store.commit('setEntries', {module_name: entry.module, entries: updated_entries});
            axios.post("updateentry", {
                'module': entry.module,
                'entry_id': entry.id,
                'start_time': this.parseTime(entry.start_time).getTime(),
                'end_time': this.parseTime(entry.end_time).getTime(),
                'title': entry.title || "Placeholder",
                'issue_id': (entry.issue || {}).id,
                'project_id': entry.project || "0",
                'extra_data': entry.extra_data,
                'text': entry.text,
            }).then(response => {
                console.log(response)
                updated_entries = this.$store.state.modules[entry.module].entries.filter((_entry) => _entry.id !== entry.id && _entry.id !== placeholderid);
                updated_entries.push(response.data)
                this.$store.commit('setInput', {title: response.data.title, issue: null})
                this.$store.commit('setEntries', {module_name: entry.module, entries: updated_entries});
                this.$store.commit('setLoading', {module_name: 'updateentry', loading: false});
                this.$store.commit('setSelectedEntry', updated_entries.find((_entry) => _entry.title === response.data.title && _entry.start_time === response.data.start_time && _entry.end_time === response.data.end_time));
            }).catch(_handleFailure)
        },
        deleteEntry(entry) {
            this.$store.commit('setLoading', {module_name: 'deleteentry', loading: true});
            updated_entries = this.$store.state.modules[entry.module].entries.filter((_entry) => _entry.id !== entry.id);
            this.$store.commit('setEntries', {module_name: entry.module, entries: updated_entries});
            this.$store.commit('setSelectedEntry', null)
            axios.post('deleteentry', {
                'module': entry.module,
                'entry_id': entry.id
            }).then((response) => {
                console.log("deleted entry " + entry.id + ": " + response.data);
                updated_entries = this.$store.state.modules[entry.module].entries.filter((_entry) => _entry.id !== entry.id);
                this.$store.commit('setInput', {title: null, issue: null})
                this.$store.commit('setEntries', {module_name: entry.module, entries: updated_entries});
                this.$store.commit('setLoading', {module_name: 'deleteentry', loading: false});
            }).catch(_handleFailure)
        },
        setToolbarHeight(event) {
            this.toolbarHeight = `${event.height}px`;
            if (event.separator) {
                this.toolbarSepHeight = `${event.height}px`;
            }
        },
        updateWeek() {
            for (el of this.$refs.daytimelines) {
                el.$refs.timeline.unloadTimeline();
            }
            this.$store.commit('setSelectedEntry', null)
            this.$store.commit('setInput', {title: null, issue: null})
            this.fetchModule("all", 0)
            this.fetchSuggestions()
        },
        fetchSuggestions() {
            axios.get("suggestions", {params: {
                from: this.$store.getters.getFrom,
                to: this.$store.getters.getTo,
            }}).then(response => {
                this.$store.commit('setSuggestions', response.data || []);
            }).catch(() => {});
        },
        estimatePopulateSeconds(from, to) {
            // Each weekday is a separate Codex call (~25s at effort=none on busy
            // days); weekends are mostly empty and resolve in a few seconds.
            from = from == null ? this.$store.getters.getFrom : from;
            to = to == null ? this.$store.getters.getTo : to;
            const DAY = 24 * 3600 * 1000;
            let secs = 6;
            for (let t = from; t < to; t += DAY) {
                const dow = new Date(t).getDay(); // 0 Sun .. 6 Sat
                secs += (dow === 0 || dow === 6) ? 6 : 25;
            }
            return secs;
        },
        fmtTime(sec) {
            sec = Math.max(0, Math.round(sec));
            const m = Math.floor(sec / 60);
            const s = sec % 60;
            return m + ":" + (s < 10 ? "0" : "") + s;
        },
        startPopulateProgress(from, to, label) {
            this.stopPopulateProgress();
            const eta = this.estimatePopulateSeconds(from, to);
            const startedAt = Date.now();
            this.populate.active = true;
            this.populate.cancelling = false;
            this.populate.progress = 0;
            this.populate.elapsedSec = 0;
            this.populate.etaSec = eta;
            this.populate.done = 0;
            this.populate.total = 0;
            this.populate.dayStartedAt = startedAt;
            this.populate.label = label || 'this week';
            this.populate.timer = setInterval(() => {
                this.populate.elapsedSec = (Date.now() - startedAt) / 1000;
                // Real floor from completed days; smooth creep within the current
                // day toward the next day's floor, so the bar is anchored to truth
                // (per-day completions) but never visibly stalls between them.
                const total = this.populate.total || Math.max(1, eta / 25);
                const floor = (this.populate.done / total) * 100;
                const ceil = ((this.populate.done + 1) / total) * 100;
                const perDay = Math.max(8, eta / total);
                const inDay = (Date.now() - this.populate.dayStartedAt) / 1000;
                const frac = Math.min(0.95, inDay / perDay);
                this.populate.progress = Math.min(99, floor + (ceil - floor) * frac);
            }, 250);
        },
        stopPopulateProgress(done) {
            if (this.populate.timer) {
                clearInterval(this.populate.timer);
                this.populate.timer = null;
            }
            this.populate.controller = null;
            if (done) {
                this.populate.progress = 100;
                setTimeout(() => { this.populate.active = false; }, 700);
            } else {
                this.populate.active = false;
            }
        },
        cancelPopulate() {
            this.populate.cancelling = true;
            if (this.populate.controller) {
                this.populate.controller.abort();
            }
        },
        dayBounds(day) {
            const parts = String(day).split('-').map(Number);
            const start = new Date(parts[0], parts[1] - 1, parts[2]);
            const end = new Date(parts[0], parts[1] - 1, parts[2] + 1);
            return {from: start.getTime(), to: end.getTime()};
        },
        populatePayload(from, to, singleDay) {
            return {
                from: from,
                to: to,
                replace_existing: true,
                single_day: Boolean(singleDay),
                // Week fill leaves already-filled days alone; Fill day replaces.
                skip_filled_days: !singleDay,
            };
        },
        codexEntry(req) {
            // req: {click(ms), prev_end(ms|null), next_start(ms|null), group}
            const group = req.group;
            const HALF = 30 * 60 * 1000; // placeholder = click ±30min (1h), clamped
            let startMs = req.click - HALF;
            let endMs = req.click + HALF;
            if (req.prev_end) startMs = Math.max(startMs, req.prev_end);
            if (req.next_start) endMs = Math.min(endMs, req.next_start);
            const start = new Date(startMs);
            const end = new Date(endMs);
            const placeholderid = "codex-pending-" + Math.random();
            const removePlaceholder = () => {
                const kept = (this.$store.state.modules[group].entries || [])
                    .filter((x) => x.id !== placeholderid);
                this.$store.commit('setEntries', {module_name: group, entries: kept});
            };
            // Optimistic placeholder bar in the clicked slot while Codex works.
            const optimistic = (this.$store.state.modules[group].entries || []).concat([{
                id: placeholderid,
                start_time: start,
                end_time: end,
                title: "⏳ Generating…",
                module: group,
                project: '',
                date_group: start.toISOString().split('T')[0],
            }]);
            this.$store.commit('setEntries', {module_name: group, entries: optimistic});
            this.$store.commit('setLoading', {module_name: 'populateentry', loading: true});

            axios.post("populateentry", {
                click: req.click,
                prev_end: req.prev_end,
                next_start: req.next_start,
            }).then(response => {
                const data = response.data || {};
                this.$store.commit('setLoading', {module_name: 'populateentry', loading: false});
                if (data.count > 0) {
                    // Real entry persisted server-side; reload to replace placeholder.
                    this.fetchModule(group, 0);
                    this.fetchSuggestions();
                } else {
                    // No signal near the click → blank manual entry to fill by hand.
                    removePlaceholder();
                    this.updateEntry({
                        module: group,
                        start_time: start,
                        end_time: end,
                        title: "Unnamed Entry",
                        project: '',
                    });
                }
            }).catch(err => {
                removePlaceholder();
                this.$store.commit('setLoading', {module_name: 'populateentry', loading: false});
                let msg = err.message;
                if (err.response && err.response.data) {
                    const d = err.response.data;
                    if (typeof d === 'object' && d.error) {
                        msg = d.error;
                    } else if (typeof d === 'string') {
                        try { msg = JSON.parse(d).error || d; } catch (e) { msg = d; }
                    }
                }
                alert("Create entry failed: " + (msg || "unknown error"));
            });
        },
        populateLocal() {
            const from = this.$store.getters.getFrom;
            const to = this.$store.getters.getTo;
            if (!confirm(
                "Fill this week's empty days with Kimai entries generated by Codex GPT-5.6 Sol " +
                "from git and ActivityWatch? Days that already have Kimai entries are skipped; " +
                "the new ones are written straight to Kimai. (takes up to about " +
                this.fmtTime(this.estimatePopulateSeconds(from, to)) +
                "; you can Cancel and keep finished days.)"
            )) {
                return;
            }
            this.runPopulate(from, to, false, 'this week');
        },
        populateDay(req) {
            const bounds = this.dayBounds(req.day);
            const count = req.existingCount || 0;
            const entries = count + " existing Kimai entr" +
                (count === 1 ? "y" : "ies");
            if (!confirm(
                "Replace " + entries + " on " + req.day +
                " with entries generated by Codex? This deletes them from " +
                "Kimai and writes the new ones straight there."
            )) {
                return;
            }
            this.runPopulate(bounds.from, bounds.to, true, req.day);
        },
        runPopulate(from, to, singleDay, label) {
            this.$store.commit('setLoading', {module_name: 'populatelocal', loading: true});
            this.startPopulateProgress(from, to, label);
            const controller = new AbortController();
            this.populate.controller = controller;
            const clearLoading = () =>
                this.$store.commit('setLoading', {module_name: 'populatelocal', loading: false});

            fetch("populatelocalstream", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(this.populatePayload(from, to, singleDay)),
                signal: controller.signal,
            }).then(response => {
                if (!response.ok || !response.body) {
                    throw new Error("HTTP " + response.status);
                }
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buf = "";
                let fatal = null;
                let dayErrors = [];
                let doneEvent = null;
                const pump = () => reader.read().then(({done, value}) => {
                    if (done) {
                        const failed = Boolean(fatal || dayErrors.length);
                        this.stopPopulateProgress(!failed);
                        this.fetchModule("kimai", 0);
                        this.fetchSuggestions();
                        clearLoading();
                        if (fatal) {
                            alert("Fill local failed: " + fatal);
                        } else if (dayErrors.length) {
                            alert("Fill local failed: " + dayErrors.join("; "));
                        } else if (doneEvent && !doneEvent.count &&
                                   (doneEvent.skipped || []).length) {
                            alert("Nothing to fill: every day with activity already " +
                                  "has Kimai entries (" + doneEvent.skipped.join(", ") + ").");
                        }
                        return;
                    }
                    buf += decoder.decode(value, {stream: true});
                    let nl;
                    while ((nl = buf.indexOf("\n")) >= 0) {
                        const line = buf.slice(0, nl).trim();
                        buf = buf.slice(nl + 1);
                        if (!line) continue;
                        let ev;
                        try { ev = JSON.parse(line); } catch (e) { continue; }
                        console.log("populate stream", ev);
                        if (ev.type === "progress") {
                            this.populate.total = ev.total;
                            this.populate.done = ev.done;
                            this.populate.dayStartedAt = Date.now();
                            this.fetchModule("kimai", 0); // surface each finished day live
                        } else if (ev.type === "day_error") {
                            console.warn("day failed", ev.day, ev.error);
                            dayErrors.push(ev.day + ": " + ev.error);
                        } else if (ev.type === "done") {
                            doneEvent = ev;
                        } else if (ev.type === "error") {
                            fatal = ev.error;
                        }
                    }
                    return pump();
                });
                return pump();
            }).catch(err => {
                if (err.name === "AbortError") {
                    // Cancelled: backend stops after the in-flight day; keep finished days.
                    this.stopPopulateProgress(false);
                    this.fetchModule("kimai", 0);
                    clearLoading();
                    return;
                }
                this.stopPopulateProgress(false);
                alert("Fill local failed: " + (err.message || "unknown error"));
                clearLoading();
            });
        },
    },
    watch: {
        "$store.state.currentWeek"() {
            if (!this.$refs.daytimelines) {
                return;
            }
            this.debounceUpdateWeek();
        }
    },
    mounted() {
        axios.get("listmodules").then(response => {
            console.log(response)
            this.$store.commit('updateModules', response.data);
        })
        axios.get("getsettings").then(response => {
            console.log(response)
            this.$store.commit('setSettings', response.data);
        })
        this.fetchSuggestions();
        this.$store.commit('setLoading', {module_name: 'fetchdata', loading: true});
        axios.get("fetchdata", {params: {
                parse: "0",
                from: this.$store.getters.getFrom,
                to: this.$store.getters.getTo,
            }}).then(response => {
            console.log("fetchdata (parse: 0)", response)
            this.$store.commit('updateModules', response.data);
            this.$store.commit('setLoading', {module_name: 'fetchdata', loading: false});
        }).catch(() => {
            this.$store.commit('setLoading', {module_name: 'fetchdata', loading: false});
        })
    }
});
