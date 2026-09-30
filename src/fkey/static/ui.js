// Browse UI behavior: the collection grid (AG Grid), search, and shortcuts.
(() => {
  const search = document.getElementById("search");
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
  // Phones: nothing is pinned and row numbers are hidden, so a sideways
  // swipe moves the whole row and every column gets the full screen width.
  const narrow = matchMedia("(max-width: 760px)").matches;
  const pinned = narrow ? null : "left";
  const AUTO_WIDTH_LIMITS = { primary: narrow ? 200 : 360, pills: 520 };
  const AUTO_WIDTH_LIMIT = 320;

  document.addEventListener("keydown", (event) => {
    const target = event.target;
    const typing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement;
    if (event.key !== "/" || !search || typing || event.metaKey || event.ctrlKey) return;
    event.preventDefault();
    search.focus();
    search.select();
  });

  document.addEventListener("click", (event) => {
    const toggle = event.target.closest(".nav-toggle");
    if (!toggle) return;
    const open = document.body.classList.toggle("nav-open");
    toggle.setAttribute("aria-expanded", String(open));
  });

  function createGrid(data) {
    const status = document.getElementById("grid-status");
    const widthsKey = `fkey:column-widths:${data.collection}`;
    const savedWidths = readJSON(widthsKey);
    const rows = data.rows.map((row) => ({ ...row, text: searchText(row) }));
    let query = normalize(search ? search.value : "");

    const api = agGrid.createGrid(document.getElementById("grid"), {
      theme: "legacy",
      rowData: rows,
      getRowId: (params) => params.data.id,
      columnDefs: [rowNumberColumn(), ...data.columns.map(columnDefinition)],
      defaultColDef: {
        resizable: true,
        sortable: true,
        suppressMovable: true,
        minWidth: 72,
        comparator: compareValues,
      },
      rowHeight: 42,
      headerHeight: 40,
      // Cut-off cells show their full value (see isCut); you can hover into
      // the tooltip to select and copy it.
      tooltipShowDelay: 350,
      tooltipInteraction: true,
      localeText: { noRowsToShow: `No ${data.noun} match` },
      isExternalFilterPresent: () => query !== "",
      doesExternalFilterPass: (node) => node.data.text.includes(query),
      onFirstDataRendered: (event) => fitColumns(event.api),
      onModelUpdated: (event) => {
        event.api.refreshCells({ columns: ["_row"], force: true });
        showStatus(event.api);
      },
      onColumnResized: (event) => {
        // Remember only columns you drag; the rest keep fitting their content.
        if (!event.finished || event.source !== "uiColumnResized") return;
        for (const column of event.columns || []) {
          savedWidths[column.getColId()] = column.getActualWidth();
        }
        writeJSON(widthsKey, savedWidths);
      },
      onRowClicked: (event) => {
        if (event.event.target.closest("a")) return;
        openRecord(event.data.href, event.event);
      },
      onCellKeyDown: (event) => {
        if (event.event.key === "Enter" && event.data) openRecord(event.data.href, event.event);
      },
    });

    if (search) {
      search.form.addEventListener("submit", (event) => event.preventDefault());
      search.addEventListener("input", () => {
        query = normalize(search.value);
        api.onFilterChanged();
        const url = new URL(location.href);
        if (search.value.trim()) url.searchParams.set("q", search.value.trim());
        else url.searchParams.delete("q");
        history.replaceState(null, "", url);
      });
      search.addEventListener("keydown", (event) => {
        if (event.key !== "Escape" || !search.value) return;
        search.value = "";
        search.dispatchEvent(new Event("input"));
      });
    }

    function columnDefinition(column) {
      const definition = {
        colId: column.key,
        headerName: column.label,
        headerComponentParams: {
          innerHeaderComponent: HeaderLabel,
          icon: column.icon,
          description: column.description,
        },
        valueGetter: (params) => params.data.values[column.key],
        tooltipValueGetter: (params) => (isCut(params) ? tooltipText(params.value) : undefined),
        width: savedWidths[column.key],
        cellClass: `cell-${column.kind}`,
      };
      if (column.kind === "primary") {
        Object.assign(definition, { pinned, lockPinned: true, cellRenderer: primaryCell });
      } else if (column.kind === "number") {
        Object.assign(definition, {
          type: "rightAligned",
          cellClass: ["cell-number", "ag-right-aligned-cell"],
        });
      } else if (column.kind === "pills") {
        Object.assign(definition, { sortable: false, cellRenderer: pillsCell });
      } else if (column.kind === "text") {
        definition.cellRenderer = textCell;
      }
      return definition;
    }

    function fitColumns(gridApi) {
      const unsized = data.columns.filter((column) => !(column.key in savedWidths));
      if (!unsized.length) return;
      gridApi.autoSizeColumns(unsized.map((column) => column.key));
      gridApi.setColumnWidths(
        unsized.map((column) => ({
          key: column.key,
          newWidth: Math.min(
            gridApi.getColumn(column.key).getActualWidth(),
            AUTO_WIDTH_LIMITS[column.kind] || AUTO_WIDTH_LIMIT,
          ),
        })),
      );
    }

    function showStatus(gridApi) {
      const shown = gridApi.getDisplayedRowCount();
      if (!query) status.textContent = `${rows.length} ${data.noun}`;
      else if (!shown) status.textContent = `No ${data.noun} match “${search.value.trim()}”`;
      else status.textContent = `${shown} of ${rows.length} ${data.noun}`;
    }
  }

  function rowNumberColumn() {
    return {
      colId: "_row",
      headerName: "",
      valueGetter: (params) => params.node.rowIndex + 1,
      width: 56,
      minWidth: 48,
      hide: narrow,
      pinned,
      lockPinned: true,
      resizable: false,
      sortable: false,
      suppressNavigable: true,
      cellClass: "cell-row-number",
    };
  }

  // A header label with a field-type icon, inside AG Grid's own header
  // (which keeps sorting and resizing).
  class HeaderLabel {
    init(params) {
      this.gui = document.createElement("span");
      this.gui.className = "header-label";
      if (params.description) this.gui.title = params.description;
      if (params.icon) this.gui.append(iconElement(params.icon));
      const text = document.createElement("span");
      text.textContent = params.displayName;
      this.gui.append(text);
    }

    getGui() {
      return this.gui;
    }

    refresh() {
      return false;
    }
  }

  // AG Grid's own "whenTruncated" mode skips cells with custom renderers,
  // which all of ours are, so measure the rendered cell directly.
  function isCut(params) {
    const row = CSS.escape(params.node.id);
    const column = CSS.escape(params.column.getColId());
    const cell = document.querySelector(`#grid .ag-row[row-id="${row}"] .ag-cell[col-id="${column}"]`);
    return Boolean(cell) && cell.scrollWidth > cell.clientWidth + 1;
  }

  function tooltipText(value) {
    if (value === null || value === undefined || value === "") return undefined;
    if (!Array.isArray(value)) return String(value);
    return value.length ? value.map((pill) => `${pill.key}: ${pill.value}`).join("\n") : undefined;
  }

  function primaryCell(params) {
    const link = document.createElement("a");
    link.className = "primary-link";
    link.href = params.data.href;
    link.textContent = params.value;
    return link;
  }

  function textCell(params) {
    const value = params.value;
    if (typeof value !== "string" || !isUrl(value)) return value ?? "";
    return externalLink(value, value);
  }

  function pillsCell(params) {
    const pills = document.createElement("span");
    pills.className = "pills";
    for (const pill of params.value || []) {
      const element = pill.url ? externalLink(pill.url) : document.createElement("span");
      element.classList.add("pill");
      element.title = `${pill.key}: ${pill.value}`;
      const key = document.createElement("span");
      key.className = "pill-key";
      key.textContent = pill.key;
      element.append(key, " ", pill.value);
      pills.append(element);
    }
    return pills;
  }

  function externalLink(href, text) {
    const link = document.createElement("a");
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    if (text) link.textContent = text;
    return link;
  }

  function iconElement(name) {
    const namespace = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(namespace, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(namespace, "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }

  function compareValues(a, b, nodeA, nodeB, descending) {
    const emptyA = a === null || a === undefined || a === "" || (Array.isArray(a) && !a.length);
    const emptyB = b === null || b === undefined || b === "" || (Array.isArray(b) && !b.length);
    // Empty cells stay at the bottom in both directions.
    if (emptyA || emptyB) return emptyA === emptyB ? 0 : (emptyA ? 1 : -1) * (descending ? -1 : 1);
    if (typeof a === "number" && typeof b === "number") return a - b;
    return collator.compare(String(a), String(b));
  }

  function searchText(row) {
    const parts = [row.id];
    for (const value of Object.values(row.values)) {
      if (Array.isArray(value)) value.forEach((pill) => parts.push(pill.key, pill.value));
      else if (value !== null && value !== undefined) parts.push(String(value));
    }
    return normalize(parts.join(" "));
  }

  function normalize(text) {
    return text.trim().toLocaleLowerCase();
  }

  function isUrl(value) {
    return /^https?:\/\/\S+$/.test(value);
  }

  function openRecord(href, event) {
    if (event.metaKey || event.ctrlKey) window.open(href, "_blank", "noopener");
    else location.href = href;
  }

  function readJSON(key) {
    try {
      return JSON.parse(localStorage.getItem(key)) || {};
    } catch {
      return {};
    }
  }

  function writeJSON(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // Private windows may refuse storage; widths then last for the visit.
    }
  }

  const dataElement = document.getElementById("grid-data");
  if (dataElement && window.agGrid) createGrid(JSON.parse(dataElement.textContent));
})();
