// Shared by question_screen.html, story_screen.html, objloc_screen.html.
// Generic CRUD's own crud.js is untouched -- these are custom, non-
// reflection-driven screens, so they don't share its table-agnostic logic.

function toggleNewAppForm(formId) {
    const form = document.getElementById(formId);
    if (form) form.classList.toggle('hidden');
}

// The <select> itself lives inside the same *-screen wrapper it needs to
// replace, so its own closest "[id$='-screen']" ancestor is always the
// right swap target regardless of which of the 3 screens called this.
function selectApp(selectEl, endpoint) {
    const screen = selectEl.closest('[id$="-screen"]');
    if (!screen) return;
    htmx.ajax('GET', `${endpoint}?app_id=${encodeURIComponent(selectEl.value)}`, {
        target: '#' + screen.id,
        swap: 'outerHTML'
    });
}

function selectRoom(appId, roomId) {
    htmx.ajax('GET', `/objloc/select?app_id=${appId}&room_id=${roomId}`, {
        target: '#objloc-screen',
        swap: 'outerHTML'
    });
}

// Pure client-side expand/collapse -- the whole tree is already in the
// DOM (rendered server-side in one shot), so toggling a branch is just a
// visibility flip, no round-trip needed.
function toggleTreeNode(btn) {
    const li = btn.closest('.tree-node');
    const childList = li && li.querySelector(':scope > ul.tree-children');
    if (!childList) return;
    const collapsed = childList.classList.toggle('hidden');
    btn.textContent = collapsed ? '+' : '−';
}

// ----------------------------
// Shared audio-file picker (one <dialog> in menu_base.html, reused by
// every audio_file_url field across Question/Story/Worlds instead of
// authors hand-typing raw URLs).
// ----------------------------
let _audioPickerTargetInputId = null;

function openAudioPicker(inputId) {
    _audioPickerTargetInputId = inputId;
    const dialog = document.getElementById('audio-picker-dialog');
    if (!dialog) return;
    htmx.ajax('GET', '/audio_picker', { target: '#audio-picker-list', swap: 'innerHTML' });
    dialog.showModal();
}

function closeAudioPicker() {
    const dialog = document.getElementById('audio-picker-dialog');
    if (dialog) dialog.close();
}

function selectAudioFile(url) {
    if (_audioPickerTargetInputId) {
        const input = document.getElementById(_audioPickerTargetInputId);
        if (input) {
            input.value = url;
            // In case a picked field is ever wired to hx-trigger="change"
            // for auto-save (none are today) -- harmless otherwise.
            input.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }
    closeAudioPicker();
}

// ----------------------------
// "Saved" feedback -- every mutation across all 3 screens (add/edit/
// delete/set_correct/toggle) is a POST that swaps the *-screen div;
// every pure navigation (select an app, open a question, go back) is a
// GET. That distinction alone is enough to flash "Saved" only after a
// real change, generically, with no changes needed to any individual
// route or template. Errors already surface via crud.js's own
// htmx:responseError listener (body-level, applies here too) -- this
// only adds the missing success side, matching that same "model the
// crud's own behavior" request.
// ----------------------------
document.body.addEventListener('htmx:afterRequest', function (evt) {
    const verb = evt.detail.requestConfig && evt.detail.requestConfig.verb;
    if (verb === 'post' && evt.detail.successful) {
        showSavedFlash();
    }
});

let _savedFlashTimeout = null;

function showSavedFlash() {
    let flash = document.getElementById('saved-flash');
    if (!flash) {
        flash = document.createElement('div');
        flash.id = 'saved-flash';
        flash.textContent = 'Saved';
        document.body.appendChild(flash);
    }
    flash.classList.add('show');
    clearTimeout(_savedFlashTimeout);
    _savedFlashTimeout = setTimeout(() => flash.classList.remove('show'), 1400);
}
