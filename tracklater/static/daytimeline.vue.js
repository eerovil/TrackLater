var daytimeline = Vue.component("daytimeline", {
    template: `
    <div>
    <div style="display:flex; justify-content:space-between; align-items:center;
        padding:2px 10px; font-size:13px; font-weight:600; color:#555;">
        <span>{{ dayDate }}</span>
        <span style="display:flex; align-items:center; gap:8px;">
          <span>{{ dayHours }} h</span>
          <v-btn
            v-if="modules.kimai"
            x-small
            color="primary"
            :loading="populateLoading"
            :disabled="populateLoading"
            @click="fillDay"
          >Fill day</v-btn>
        </span>
    </div>
    <vuetimeline ref="timeline"
    :items="items"
    :groups="groups"
    :options="options"
    :events="['select']"
    :selection=selection
    @select="select">
    </vuetimeline>
    </div>
    `,
    props: ["entries"],
    data() {
      return {
        items: [],
      }
    },
    mounted() {
      this.items = this.entriesToItems(this.entries);
    },
    methods: {
      fillDay() {
        this.$emit('fillDay', {
          day: this.dayDate,
          existingCount: (this.entries || []).filter(
            (e) => e.module === 'kimai',
          ).length,
        });
      },
      myChangedCallback(arg1, arg2, arg3) {
        console.log(arg1, arg2, arg3)
      },
      select(props) {
        const entry = this.entries[props.items[0]] || null;
        if (entry != null) {
          this.$store.commit('setInput', {title: entry.title, issue: entry.issue || this.findIssue(entry.title)})
          this.$store.commit('setSelectedEntry', entry);
        }
      },
      findIssue(title) {
        return this.$store.getters.findIssue(title)
      },
      onMove: function(item, callback) {
        if (!this.modules[item.group].capabilities.includes('updateentry')) {
            if (callback) callback(null);
            return;
        }
        const MS30 = 30 * 60 * 1000;
        const snap = (d) => Math.round(d.getTime() / MS30) * MS30;
        const entry = this.entries[item.id];
        const oldStart = new Date(entry.start_time).getTime();
        const oldEnd = new Date(entry.end_time).getTime();
        const movedStart = item.start.getTime() !== oldStart;
        // Compare against the bar as drawn (kimai bars end 1s late), or an
        // untouched end reads as moved and a left-edge resize becomes a move.
        const movedEnd = item.end.getTime() !== this.timelineEndForEntry(entry).getTime();
        let s = snap(item.start);
        let e = snap(item.end);
        if (movedStart && movedEnd) {
            // Whole-item move: keep the (snapped) duration, never below 30 min.
            e = s + Math.max(MS30, oldEnd - oldStart);
        } else if (e - s < MS30) {
            // Resize that collapsed below the minimum: hold the edge not dragged.
            if (movedStart) { s = e - MS30; } else { e = s + MS30; }
        }
        if (s === oldStart && e === oldEnd) {
            if (callback) callback(null);
            return;
        }
        entry.start_time = new Date(s);
        entry.end_time = new Date(e);
        item.start = entry.start_time;
        item.end = this.timelineEndForEntry(entry);
        if (callback) callback(item); // reflect the snapped position immediately
        this.$emit('updateEntry', entry);
        this.$store.commit('setSelectedEntry', entry);
      },
      onRemove: function(item, callback) {
          if (this.modules[item.group].capabilities.includes('deleteentry')) {
              let entry = this.entries[item.id];
              this.$emit('deleteEntry', entry)
          }
      },
      onAdd: function(item, callback) {
          // Double-click → ask Codex to grow a single entry from the click
          // seed (replaces the old generateTimeSnippet/detectIssue heuristic).
          if (!this.modules[item.group].capabilities.includes('addentry')) {
              if (callback) callback(null);
              return;
          }
          const click = item.start.getTime();
          // Bound the new entry by the neighbouring entries of the SAME module so
          // it can't overlap them.
          let prevEnd = null, nextStart = null;
          this.entries.forEach((e) => {
              if (e.module !== item.group || !e.end_time) return;
              const s = new Date(e.start_time).getTime();
              const en = new Date(e.end_time).getTime();
              if (en <= click && (prevEnd === null || en > prevEnd)) prevEnd = en;
              if (s >= click && (nextStart === null || s < nextStart)) nextStart = s;
          });
          if (callback) callback(null); // cancel vis's default item; Codex creates it
          this.$emit('codexEntry', {
              click: click,
              prev_end: prevEnd,
              next_start: nextStart,
              group: item.group,
          });
      },
      timelineEndForEntry(entry) {
        // vis.js treats range end as exclusive; extend local bars by 1s so
        // commits on the end timestamp still appear inside the block.
        const end = new Date(entry.end_time);
        if (entry.module === 'kimai') {
          return new Date(end.getTime() + 1000);
        }
        return end;
      },
      logCommitsOutsideLocalEntries(entries) {
        const localRows = entries.filter(
          (e) => e.module === 'kimai' && e.end_time,
        );
        const gitRows = entries.filter((e) => e.module === 'gitmodule');
        const orphans = [];
        gitRows.forEach((gitEntry) => {
          const projectId = this.$store.getters.getProjectId(
            gitEntry.group, 'kimai',
          );
          if (!projectId) {
            return;
          }
          const t = new Date(gitEntry.start_time).getTime();
          const covering = localRows.filter((l) => l.project === projectId);
          const insideExclusive = covering.some((l) => {
            return (
              new Date(l.start_time).getTime() <= t
              && this.timelineEndForEntry(l).getTime() > t
            );
          });
          if (insideExclusive) {
            return;
          }
          const insideInclusive = covering.some((l) => {
            return (
              new Date(l.start_time).getTime() <= t
              && new Date(l.end_time).getTime() >= t
            );
          });
          orphans.push({
            commit: gitEntry.start_time,
            group: gitEntry.group,
            projectId,
            onBlockEnd: insideInclusive && !insideExclusive,
          });
        });
        if (orphans.length) {
          console.warn(
            '[TrackLater] Git commits not inside local blocks on timeline:',
            orphans,
          );
        }
      },
      entriesToItems(entries) {
        if (!entries.length) {
          return [];
        }
        const items = entries.map((entry, i) => {
          let row = {
            id: i,
            group: entry.module,
            start: new Date(entry.start_time),
            className: entry.module,
            content: entry.title,
            title: (entry.text || "").replace(/(?:\r\n|\r|\n)/g, '<br />'),
            editable: {
              updateTime: this.modules[entry.module].capabilities.includes('updateentry'),
              remove: this.modules[entry.module].capabilities.includes('deleteentry')
            },
          }
          if (entry.id && entry.id.startsWith("placeholderid")) {
            row.editable = false
            row.selectable = false;
          }
          if (entry.is_draft) {
            row.className += ' draft';
          }
          let colorObj = this.modules[entry.module].color;
          color = colorObj[entry.group] || colorObj.global;
          if (entry.end_time != undefined) {
              row.end = this.timelineEndForEntry(entry);
              if (color != null) {
                  row.style = `background-color: ${color}`
              }
          } else {
              row.type = 'point'
              if (color != null) {
                  row.className += ` point-color-${color}`
              }
          }
          return row
        });
        this.logCommitsOutsideLocalEntries(entries);
        return items;
      },
    },
    watch: {
      entries(entries, oldEntries) {
          if (_.isEqual(entries, oldEntries)) {
            return
          }
          this.items = this.entriesToItems(entries);
        }
    },
    computed: {
      selection() {
        const selectedEntry = this.$store.state.selectedEntry;
        if (selectedEntry != null && selectedEntry.date_group === (this.entries[0] || {}).date_group) {
          for (let i=0; i<this.entries.length; i++) {
            if (this.entries[i].module == selectedEntry.module &&
                selectedEntry.id != null &&
                this.entries[i].id === selectedEntry.id) {
                  return i;
                }
          }
        }
      },
      modules() {
          return this.$store.state.modules;
      },
      dayDate() {
        return (this.entries[0] || {}).date_group || '';
      },
      dayHours() {
        // Billed hours for the day = sum of kimai (manual billing) entry spans.
        const secs = (this.entries || [])
          .filter((e) => e.module === 'kimai' && e.end_time)
          .reduce((acc, e) =>
            acc + (new Date(e.end_time).getTime() - new Date(e.start_time).getTime()) / 1000, 0);
        return Math.round((secs / 3600) * 10) / 10;
      },
      populateLoading() {
        return Boolean(this.$store.state.loading.populatelocal);
      },
      timeEntryModules() {
        return Object.keys(this.modules).filter(key => this.modules[key].capabilities.includes("entries"));
      },
      groups() {
        ret = []
        for (let module_name in this.modules) {
          if (this.modules[module_name].capabilities.includes('entries')){
            ret.push({
              id: module_name,
              content: module_name,
            })
          }
        }
        return ret
      },
      options() {
        self = this
        let firstDate = new Date(this.entries[0].date_group);
        const day_start = firstDate.setHours(6, 0, 0, 0);
        const day_end = firstDate.setHours(26, 0, 0, 0);

        return {
          start: day_start,
          end: day_end,
          editable: true,
          zoomable: (screen.width < 960),
          showCurrentTime: false,
          horizontalScroll: false,
          moveable: true,
          margin: {
              item: 0
          },
          snap: function(date, scale, step) {
            // Live-snap dragging/resizing to 30-minute increments.
            const MS30 = 30 * 60 * 1000;
            return new Date(Math.round(date.getTime() / MS30) * MS30);
          },
          onMove: self.onMove,
          onRemove: self.onRemove,
          onAdd:self.onAdd,
          tooltip: {
            delay: 1
          }
        }
      }
    },
});
