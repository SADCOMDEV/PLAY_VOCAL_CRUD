// Only declare selectedRowId once globally
if (typeof window.selectedRowId === "undefined") {
    window.selectedRowId = null;
}

// ----------------------------
// Toolbar lock/unlock
// ----------------------------
function lockToolbar() {
    const tableSelect = document.querySelector('#table-select');
    if (tableSelect) tableSelect.disabled = true;

    document.querySelectorAll('#add-btn, #edit-btn, #delete-btn').forEach(b => b.disabled = true);
}

function unlockToolbar() {
    const tableSelect = document.querySelector('#table-select');
    if (tableSelect) tableSelect.disabled = false;

    document.querySelectorAll('#add-btn, #edit-btn, #delete-btn').forEach(b => b.disabled = false);
}

// ----------------------------
// Row selection
// ----------------------------
function setSelectedRow(id) {
    const tableSelect = document.querySelector('#table-select');
    if (tableSelect && tableSelect.disabled) return;

    window.selectedRowId = id;

    document.querySelectorAll('#crud-container table tr').forEach(tr => tr.classList.remove('selected'));
    const row = document.querySelector(`#crud-container table tr[data-row-id="${id}"]`);
    if (row) row.classList.add('selected');
}

function rebindRowSelection() {
    document.querySelectorAll('#crud-container table tbody tr input[type="radio"]').forEach(input => {
        const rowId = input.value;
        const tr = input.closest('tr');
        tr.setAttribute('data-row-id', rowId);
        input.onclick = () => setSelectedRow(rowId);
    });
}

function clearSelection() {
    if (window.selectedRowId !== null) {
        const radio = document.querySelector(`#crud-container table tr[data-row-id="${window.selectedRowId}"] input[type="radio"]`);
        if (radio) radio.checked = false;

        const row = document.querySelector(`#crud-container table tr[data-row-id="${window.selectedRowId}"]`);
        if (row) row.classList.remove('selected');

        window.selectedRowId = null;
    }
}

// ----------------------------
// Edit / Delete
// ----------------------------
function editSelected() {
    if (!window.selectedRowId) { alert("Select a row first"); return; }
    const table = document.querySelector('#table-select').value;
    lockToolbar();
    htmx.ajax('GET', `/crud_edit_form?table=${table}&id=${window.selectedRowId}`, '#crud-content', 'innerHTML');
}

function deleteSelected() {
    if (!window.selectedRowId) { alert("Select a row first"); return; }
    const table = document.querySelector('#table-select').value;
    if (confirm("Are you sure you want to delete this row?")) {
        htmx.ajax('POST', `/crud/delete`, {
            target: '#crud-content',
            swap: 'innerHTML',
            values: { table: table, id: window.selectedRowId }
        });
        window.selectedRowId = null;
    }
}

// ----------------------------
// Save edit / insert
// ----------------------------
function saveEdit() {
    const editWrapper = document.querySelector('.edit-wrapper');
    if (!editWrapper) return;

    const table = editWrapper.dataset.table;
    const rowId = editWrapper.dataset.rowId;
    const isInsert = editWrapper.dataset.insert === 'true';

    const formData = new FormData();
    formData.append("table", table);
    if (!isInsert) formData.append("id", rowId);

    let emptyRequired = false;

    editWrapper.querySelectorAll('input').forEach(input => {
        if (input.disabled) return; // skip locked fields

        if (input.hasAttribute('required') && !input.value.trim()) {
            emptyRequired = true;
            input.style.border = "2px solid yellow";
        } else {
            input.style.border = "";
        }

        if (input.type === 'checkbox') {
            formData.append(input.name, input.checked ? 'true' : 'false');
        } else {
            formData.append(input.name, input.value);
        }
    });

    if (emptyRequired) {
        showCrudError("Please fill all required fields before saving.");
        return;
    }

    const prevSelected = window.selectedRowId;
    const endpoint = isInsert ? '/crud/insert' : '/crud/edit';

    htmx.ajax('POST', endpoint, {
        target: '#crud-content',
        swap: 'innerHTML',
        values: Object.fromEntries(formData),
        afterSwap: () => {
            if (editWrapper) editWrapper.remove();

            if (prevSelected) {
                const oldRow = document.querySelector(`#crud-container table tr[data-row-id="${prevSelected}"]`);
                if (oldRow) oldRow.classList.remove('selected');
            }

            window.selectedRowId = null;
            rebindRowSelection();
            unlockToolbar();
        },
        headers: { 'HX-Trigger': 'checkForError' }
    });
}

function cancelEdit() {
    const editWrapper = document.querySelector('.edit-wrapper');
    if (editWrapper) editWrapper.remove();

    unlockToolbar();

    const tableSelect = document.querySelector('#table-select');
    if (tableSelect) {
        const table = tableSelect.value;
        htmx.ajax('GET', `/crud_table?selected_table=${table}`, '#crud-content', 'innerHTML');
    }
}

function bindEditForm() {
    const editForm = document.querySelector('.edit-wrapper');
    if (!editForm) return;

    const saveBtn = editForm.querySelector('.save-btn');
    const cancelBtn = editForm.querySelector('.cancel-btn');

    if (saveBtn) saveBtn.onclick = saveEdit;
    if (cancelBtn) cancelBtn.onclick = cancelEdit;
}

// ----------------------------
// HTMX event listeners
// ----------------------------
document.body.addEventListener('htmx:afterSwap', (evt) => {
    if (evt.target.id === 'crud-content') {
        if (evt.target.querySelector('.edit-wrapper') === null) unlockToolbar();
        rebindRowSelection();
        bindEditForm();
    }
});

document.body.addEventListener('htmx:responseError', (evt) => {
    if (evt.detail.xhr && evt.detail.xhr.responseText) {
        showCrudError(evt.detail.xhr.responseText);
    }
});

// ----------------------------
// Error popup
// ----------------------------
function showCrudError(msg) {
    let popup = document.getElementById('crud-error-popup');
    if (!popup) {
        popup = document.createElement('div');
        popup.id = 'crud-error-popup';
        popup.style = `
            position:fixed;
            top:50%;
            left:50%;
            transform:translate(-50%, -50%);
            background:#f44336;
            color:white;
            padding:30px 40px;
            font-size:18px;
            font-weight:bold;
            border-radius:8px;
            z-index:10000;
            max-width:80%;
            text-align:center;
        `;

        const header = document.createElement('div');
        header.textContent = "Database Error";
        header.style = `
            font-size:24px;
            font-weight:bold;
            margin-bottom:15px;
        `;
        popup.appendChild(header);

        const span = document.createElement('span');
        span.id = 'crud-error-text';
        popup.appendChild(span);

        const btn = document.createElement('button');
        btn.textContent = 'Close';
        btn.style = `
            display:block;
            margin:20px auto 0;
            background:white;
            color:#f44336;
            border:none;
            padding:10px 20px;
            cursor:pointer;
            font-weight:bold;
            border-radius:4px;
        `;
        btn.onclick = () => { popup.style.display = 'none'; };
        popup.appendChild(btn);

        document.body.appendChild(popup);
    }

    document.getElementById('crud-error-text').textContent = msg;
    popup.style.display = 'block';
}

// Stuff for CRUD_SWAP locking, etc
function updateHeader(tableName) {
    const headerSpan = document.getElementById("current-table-name");
    if (headerSpan) {
        headerSpan.textContent = tableName || "No Table Selected";
    }
}

// Called when user changes table from dropdown
function onTableChange(selectEl) {
    const table = selectEl.value;
    updateHeader(table);
    htmx.ajax('GET', `/crud_table?selected_table=${encodeURIComponent(table)}&sort=&sort_dir=asc`, '#crud-content', 'innerHTML');
}

// Add row handler
function addRow() {
    const tableSelect = document.querySelector('#table-select');
    if (!tableSelect || !tableSelect.value) {
        showCrudError('No table selected!');
        return;
    }
    const table = tableSelect.value;
    lockToolbar();
    htmx.ajax('GET', `/crud_add_form?selected_table=${encodeURIComponent(table)}`, '#crud-content', 'innerHTML');
}