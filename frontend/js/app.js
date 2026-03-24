// ========== Configuration ==========
const API_BASE = window.location.origin;
const WS_BASE = API_BASE.replace(/^http/, 'ws');

// ========== State ==========
const state = {
    currentSessionId: null,
    agents: [],
    skills: [],
    ws: null,
    isStreaming: false,
    isListening: false,
    recognition: null,
    editorMode: null, // 'create-agent', 'edit-agent', 'create-skill', 'edit-skill'
    editorTarget: null,
};

// ========== DOM Elements ==========
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const els = {
    sidebar: $('#sidebar'),
    sidebarToggle: $('#sidebar-toggle'),
    messages: $('#messages'),
    chatInput: $('#chat-input'),
    sendBtn: $('#send-btn'),
    voiceBtn: $('#voice-btn'),
    voiceIndicator: $('#voice-indicator'),
    agentSelect: $('#agent-select'),
    agentHint: $('#agent-hint'),
    newChatBtn: $('#new-chat-btn'),
    sessionsList: $('#sessions-list'),
    agentsList: $('#agents-list'),
    skillsList: $('#skills-list'),
    fileTree: $('#file-tree'),
    refreshFilesBtn: $('#refresh-files-btn'),
    newAgentBtn: $('#new-agent-btn'),
    newSkillBtn: $('#new-skill-btn'),
    // Editor modal
    editorModal: $('#editor-modal'),
    editorTitle: $('#editor-title'),
    editorName: $('#editor-name'),
    editorDescription: $('#editor-description'),
    editorTools: $('#editor-tools'),
    editorToolsGroup: $('#editor-tools-group'),
    editorSkills: $('#editor-skills'),
    editorSkillsGroup: $('#editor-skills-group'),
    editorBody: $('#editor-body'),
    editorSave: $('#editor-save'),
    editorCancel: $('#editor-cancel'),
    editorClose: $('#editor-close'),
    // File preview modal
    filePreviewModal: $('#file-preview-modal'),
    filePreviewTitle: $('#file-preview-title'),
    filePreviewBody: $('#file-preview-body'),
    fileDownloadBtn: $('#file-download-btn'),
    filePreviewClose: $('#file-preview-close'),
};

// ========== API Helpers ==========
async function api(path, options = {}) {
    const url = `${API_BASE}${path}`;
    const resp = await fetch(url, {
        headers: { 'Content-Type': 'application/json', ...options.headers },
        ...options,
    });
    if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`API error ${resp.status}: ${text}`);
    }
    if (resp.status === 204) return null;
    return resp.json();
}

// ========== Markdown Rendering ==========
function renderMarkdown(text) {
    if (typeof marked !== 'undefined') {
        marked.setOptions({ breaks: true, gfm: true });
        const html = marked.parse(text);
        return html;
    }
    return escapeHtml(text).replace(/\n/g, '<br>');
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function highlightCode(el) {
    if (typeof hljs !== 'undefined') {
        el.querySelectorAll('pre code').forEach((block) => {
            hljs.highlightElement(block);
        });
    }
}

// ========== Message Rendering ==========
function addMessage(role, contents, agentName = null) {
    const msg = document.createElement('div');
    msg.classList.add('message', role);

    // Header
    if (role === 'assistant') {
        const header = document.createElement('div');
        header.classList.add('msg-header');
        header.innerHTML = agentName
            ? `<span class="msg-agent">@${escapeHtml(agentName)}</span> <span>Copilot</span>`
            : `<span>Copilot</span>`;
        msg.appendChild(header);
    }

    // Content blocks
    if (typeof contents === 'string') {
        msg.innerHTML += renderMarkdown(contents);
    } else if (Array.isArray(contents)) {
        contents.forEach((block) => {
            const el = renderContentBlock(block);
            msg.appendChild(el);
        });
    }

    els.messages.appendChild(msg);
    highlightCode(msg);
    els.messages.scrollTop = els.messages.scrollHeight;
    return msg;
}

function renderContentBlock(block) {
    const wrapper = document.createElement('div');

    switch (block.type) {
        case 'code': {
            const codeWrapper = document.createElement('div');
            codeWrapper.classList.add('code-block-wrapper');

            const header = document.createElement('div');
            header.classList.add('code-block-header');
            header.innerHTML = `
                <span>${escapeHtml(block.language || 'code')}</span>
                <button class="copy-btn" onclick="copyCode(this)">Copy</button>
            `;
            codeWrapper.appendChild(header);

            const pre = document.createElement('pre');
            const code = document.createElement('code');
            code.classList.add(block.language ? `language-${block.language}` : '');
            code.textContent = block.content;
            pre.appendChild(code);
            codeWrapper.appendChild(pre);

            wrapper.appendChild(codeWrapper);
            break;
        }
        case 'file': {
            const card = document.createElement('div');
            card.classList.add('file-card');
            card.innerHTML = `
                <span class="file-icon">📄</span>
                <span class="file-name">${escapeHtml(block.filename || 'file')}</span>
            `;
            if (block.blob_path) {
                card.onclick = () => previewFile(block.blob_path, block.filename);
            }
            wrapper.appendChild(card);
            break;
        }
        case 'command_output': {
            const pre = document.createElement('pre');
            pre.style.borderLeft = '3px solid var(--warning)';
            const code = document.createElement('code');
            code.textContent = block.content;
            pre.appendChild(code);
            wrapper.appendChild(pre);
            break;
        }
        case 'error': {
            const errDiv = document.createElement('div');
            errDiv.style.color = 'var(--error)';
            errDiv.textContent = block.content;
            wrapper.appendChild(errDiv);
            break;
        }
        default: {
            const textDiv = document.createElement('div');
            textDiv.innerHTML = renderMarkdown(block.content || '');
            wrapper.appendChild(textDiv);
        }
    }

    return wrapper;
}

function addStreamingMessage(agentName = null) {
    const msg = document.createElement('div');
    msg.classList.add('message', 'assistant');
    msg.id = 'streaming-msg';

    const header = document.createElement('div');
    header.classList.add('msg-header');
    header.innerHTML = agentName
        ? `<span class="msg-agent">@${escapeHtml(agentName)}</span> <span>Copilot</span>`
        : `<span>Copilot</span>`;
    msg.appendChild(header);

    const content = document.createElement('div');
    content.id = 'streaming-content';
    msg.appendChild(content);

    const cursor = document.createElement('span');
    cursor.classList.add('streaming-cursor');
    msg.appendChild(cursor);

    els.messages.appendChild(msg);
    els.messages.scrollTop = els.messages.scrollHeight;
    return msg;
}

function appendStreamChunk(text) {
    const content = $('#streaming-content');
    if (content) {
        content.textContent += text;
        els.messages.scrollTop = els.messages.scrollHeight;
    }
}

function finalizeStreamingMessage(contents) {
    const msg = $('#streaming-msg');
    if (!msg) return;

    // Remove cursor
    const cursor = msg.querySelector('.streaming-cursor');
    if (cursor) cursor.remove();

    // Remove raw streaming content
    const raw = $('#streaming-content');
    if (raw) raw.remove();

    // Render parsed content blocks
    if (contents && contents.length > 0) {
        contents.forEach((block) => {
            const el = renderContentBlock(block);
            msg.appendChild(el);
        });
    }

    msg.removeAttribute('id');
    highlightCode(msg);
    els.messages.scrollTop = els.messages.scrollHeight;
}

// ========== Copy Code ==========
window.copyCode = function (btn) {
    const pre = btn.closest('.code-block-wrapper').querySelector('pre code');
    navigator.clipboard.writeText(pre.textContent).then(() => {
        btn.textContent = 'Copied!';
        setTimeout(() => (btn.textContent = 'Copy'), 2000);
    });
};

// ========== WebSocket Chat ==========
function connectWebSocket() {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) return;

    state.ws = new WebSocket(`${WS_BASE}/api/chat/stream`);

    state.ws.onopen = () => console.log('WebSocket connected');

    state.ws.onmessage = (event) => {
        const data = JSON.parse(event.data);

        switch (data.type) {
            case 'session':
                state.currentSessionId = data.session_id;
                loadSessions();
                break;
            case 'chunk':
                appendStreamChunk(data.content);
                break;
            case 'done':
                state.isStreaming = false;
                finalizeStreamingMessage(data.contents);
                setInputEnabled(true);
                break;
            case 'error':
                state.isStreaming = false;
                finalizeStreamingMessage([{ type: 'error', content: data.content }]);
                setInputEnabled(true);
                break;
        }
    };

    state.ws.onclose = () => {
        console.log('WebSocket disconnected, reconnecting...');
        setTimeout(connectWebSocket, 2000);
    };

    state.ws.onerror = (err) => {
        console.error('WebSocket error:', err);
    };
}

function sendMessage() {
    const text = els.chatInput.value.trim();
    if (!text || state.isStreaming) return;

    const agentName = els.agentSelect.value || null;

    // Show user message
    addMessage('user', text);

    // Clear input
    els.chatInput.value = '';
    els.chatInput.style.height = 'auto';

    // Setup streaming
    state.isStreaming = true;
    setInputEnabled(false);
    addStreamingMessage(agentName);

    // Send via WebSocket
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({
            message: text,
            agent_name: agentName,
            session_id: state.currentSessionId,
        }));
    } else {
        finalizeStreamingMessage([{ type: 'error', content: 'Not connected. Reconnecting...' }]);
        state.isStreaming = false;
        setInputEnabled(true);
        connectWebSocket();
    }
}

function setInputEnabled(enabled) {
    els.chatInput.disabled = !enabled;
    els.sendBtn.disabled = !enabled;
    els.sendBtn.style.opacity = enabled ? '1' : '0.5';
}

// ========== Voice Input ==========
function initVoice() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
        els.voiceBtn.style.display = 'none';
        return;
    }

    state.recognition = new SpeechRecognition();
    state.recognition.continuous = true;
    state.recognition.interimResults = true;
    state.recognition.lang = 'en-US';

    state.recognition.onresult = (event) => {
        let transcript = '';
        for (let i = event.resultIndex; i < event.results.length; i++) {
            transcript += event.results[i][0].transcript;
        }
        els.chatInput.value = transcript;
    };

    state.recognition.onend = () => {
        if (state.isListening) {
            state.isListening = false;
            els.voiceBtn.classList.remove('active');
            els.voiceIndicator.classList.add('hidden');
        }
    };

    state.recognition.onerror = () => {
        state.isListening = false;
        els.voiceBtn.classList.remove('active');
        els.voiceIndicator.classList.add('hidden');
    };
}

function toggleVoice() {
    if (!state.recognition) return;

    if (state.isListening) {
        state.recognition.stop();
        state.isListening = false;
        els.voiceBtn.classList.remove('active');
        els.voiceIndicator.classList.add('hidden');
    } else {
        state.recognition.start();
        state.isListening = true;
        els.voiceBtn.classList.add('active');
        els.voiceIndicator.classList.remove('hidden');
    }
}

// ========== Sessions ==========
async function loadSessions() {
    try {
        const sessions = await api('/api/chat/sessions');
        renderSessionsList(sessions);
    } catch (e) {
        console.error('Failed to load sessions:', e);
    }
}

function renderSessionsList(sessions) {
    els.sessionsList.innerHTML = '';
    if (sessions.length === 0) {
        els.sessionsList.innerHTML = '<div style="padding: 16px; color: var(--text-secondary); font-size: 13px;">No chat history yet</div>';
        return;
    }
    sessions.forEach((s) => {
        const card = document.createElement('div');
        card.classList.add('item-card');
        if (s.id === state.currentSessionId) card.classList.add('active');
        card.innerHTML = `
            <div>
                <div class="item-name">${escapeHtml(s.title)}</div>
                <div class="item-meta">${s.message_count} messages</div>
            </div>
            <div class="item-actions">
                <button class="btn-danger" onclick="event.stopPropagation(); deleteSession('${s.id}')">🗑</button>
            </div>
        `;
        card.onclick = () => loadSession(s.id);
        els.sessionsList.appendChild(card);
    });
}

async function loadSession(sessionId) {
    try {
        const session = await api(`/api/chat/sessions/${sessionId}`);
        state.currentSessionId = sessionId;
        els.messages.innerHTML = '';
        session.messages.forEach((msg) => {
            addMessage(msg.role, msg.contents, msg.agent_name);
        });
        loadSessions(); // Refresh active state
    } catch (e) {
        console.error('Failed to load session:', e);
    }
}

async function deleteSession(sessionId) {
    try {
        await api(`/api/chat/sessions/${sessionId}`, { method: 'DELETE' });
        if (state.currentSessionId === sessionId) {
            state.currentSessionId = null;
            showEmptyState();
        }
        loadSessions();
    } catch (e) {
        console.error('Failed to delete session:', e);
    }
}

function newChat() {
    state.currentSessionId = null;
    showEmptyState();
}

function showEmptyState() {
    els.messages.innerHTML = `
        <div class="empty-state">
            <div class="empty-icon">⚡</div>
            <h2>OpenCopilot</h2>
            <p>Ask Copilot anything, run your agents with <code>/agent-name</code>, or select an agent from the dropdown above.</p>
        </div>
    `;
}

// ========== Agents ==========
async function loadAgents() {
    try {
        state.agents = await api('/api/agents');
        renderAgentsList();
        renderAgentSelect();
    } catch (e) {
        console.error('Failed to load agents:', e);
    }
}

function renderAgentsList() {
    els.agentsList.innerHTML = '';
    state.agents.forEach((a) => {
        const card = document.createElement('div');
        card.classList.add('item-card');
        card.innerHTML = `
            <div>
                <div class="item-name">🤖 ${escapeHtml(a.name)}</div>
                <div class="item-meta">${a.skills_count} skills</div>
            </div>
            <div class="item-actions">
                <button class="btn-danger" onclick="event.stopPropagation(); editAgent('${a.name}')">✏️</button>
                <button class="btn-danger" onclick="event.stopPropagation(); deleteAgent('${a.name}')">🗑</button>
            </div>
        `;
        card.onclick = () => {
            els.agentSelect.value = a.name;
            updateAgentHint();
        };
        els.agentsList.appendChild(card);
    });
}

function renderAgentSelect() {
    const current = els.agentSelect.value;
    els.agentSelect.innerHTML = '<option value="">Freeform (gh copilot)</option>';
    state.agents.forEach((a) => {
        const opt = document.createElement('option');
        opt.value = a.name;
        opt.textContent = `@${a.name}`;
        els.agentSelect.appendChild(opt);
    });
    els.agentSelect.value = current;
}

function updateAgentHint() {
    const name = els.agentSelect.value;
    const agent = state.agents.find((a) => a.name === name);
    els.agentHint.textContent = agent ? agent.description.substring(0, 100) : '';
}

async function editAgent(name) {
    try {
        const agent = await api(`/api/agents/${name}`);
        openEditor('edit-agent', agent);
    } catch (e) {
        console.error('Failed to load agent:', e);
    }
}

async function deleteAgent(name) {
    if (!confirm(`Delete agent "${name}"?`)) return;
    try {
        await api(`/api/agents/${name}`, { method: 'DELETE' });
        loadAgents();
    } catch (e) {
        console.error('Failed to delete agent:', e);
    }
}
window.editAgent = editAgent;
window.deleteAgent = deleteAgent;
window.deleteSession = deleteSession;

// ========== Skills ==========
async function loadSkills() {
    try {
        state.skills = await api('/api/skills');
        renderSkillsList();
    } catch (e) {
        console.error('Failed to load skills:', e);
    }
}

function renderSkillsList() {
    els.skillsList.innerHTML = '';
    state.skills.forEach((s) => {
        const card = document.createElement('div');
        card.classList.add('item-card');
        card.innerHTML = `
            <div>
                <div class="item-name">🧩 ${escapeHtml(s.name)}</div>
            </div>
            <div class="item-actions">
                <button class="btn-danger" onclick="event.stopPropagation(); editSkill('${s.name}')">✏️</button>
                <button class="btn-danger" onclick="event.stopPropagation(); deleteSkill('${s.name}')">🗑</button>
            </div>
        `;
        els.skillsList.appendChild(card);
    });
}

async function editSkill(name) {
    try {
        const skill = await api(`/api/skills/${name}`);
        openEditor('edit-skill', skill);
    } catch (e) {
        console.error('Failed to load skill:', e);
    }
}

async function deleteSkill(name) {
    if (!confirm(`Delete skill "${name}"?`)) return;
    try {
        await api(`/api/skills/${name}`, { method: 'DELETE' });
        loadSkills();
    } catch (e) {
        console.error('Failed to delete skill:', e);
    }
}
window.editSkill = editSkill;
window.deleteSkill = deleteSkill;

// ========== Editor Modal ==========
function openEditor(mode, data = null) {
    state.editorMode = mode;
    state.editorTarget = data;

    const isAgent = mode.includes('agent');
    const isCreate = mode.startsWith('create');

    els.editorTitle.textContent = isCreate
        ? (isAgent ? 'Create Agent' : 'Create Skill')
        : (isAgent ? 'Edit Agent' : 'Edit Skill');

    els.editorToolsGroup.style.display = isAgent ? 'block' : 'none';
    els.editorSkillsGroup.style.display = isAgent ? 'block' : 'none';

    if (isCreate) {
        els.editorName.value = '';
        els.editorName.disabled = false;
        els.editorDescription.value = '';
        els.editorTools.value = 'edit, agent, search, web';
        els.editorSkills.value = '';
        els.editorBody.value = '';
    } else {
        els.editorName.value = data.name || '';
        els.editorName.disabled = true;
        els.editorDescription.value = data.description || '';
        els.editorTools.value = (data.tools || []).join(', ');
        els.editorSkills.value = (data.skills || []).join(', ');
        els.editorBody.value = data.body || '';
    }

    els.editorModal.classList.remove('hidden');
}

function closeEditor() {
    els.editorModal.classList.add('hidden');
    state.editorMode = null;
    state.editorTarget = null;
}

async function saveEditor() {
    const name = els.editorName.value.trim();
    if (!name) return alert('Name is required');

    const isAgent = state.editorMode.includes('agent');
    const isCreate = state.editorMode.startsWith('create');

    const payload = {
        name,
        description: els.editorDescription.value.trim(),
        body: els.editorBody.value,
    };

    if (isAgent) {
        payload.tools = els.editorTools.value.split(',').map((s) => s.trim()).filter(Boolean);
        payload.skills = els.editorSkills.value.split(',').map((s) => s.trim()).filter(Boolean);
    }

    try {
        if (isAgent) {
            if (isCreate) {
                await api('/api/agents', { method: 'POST', body: JSON.stringify(payload) });
            } else {
                await api(`/api/agents/${name}`, { method: 'PUT', body: JSON.stringify(payload) });
            }
            loadAgents();
        } else {
            if (isCreate) {
                await api('/api/skills', { method: 'POST', body: JSON.stringify(payload) });
            } else {
                await api(`/api/skills/${name}`, { method: 'PUT', body: JSON.stringify(payload) });
            }
            loadSkills();
        }
        closeEditor();
    } catch (e) {
        alert('Failed to save: ' + e.message);
    }
}

// ========== File Explorer ==========
async function loadFileTree() {
    try {
        const tree = await api('/api/files/tree');
        renderFileTree(tree, els.fileTree, 0);
    } catch (e) {
        els.fileTree.innerHTML = '<div style="padding: 16px; color: var(--text-secondary);">No files yet</div>';
    }
}

function renderFileTree(nodes, container, depth) {
    container.innerHTML = '';
    nodes.forEach((node) => {
        const item = document.createElement('div');
        item.classList.add('tree-item');
        item.style.setProperty('--depth', depth);

        const icon = node.is_folder ? '📁' : getFileIcon(node.name);
        item.innerHTML = `
            <span class="tree-icon">${node.is_folder ? '▶' : ''} ${icon}</span>
            <span class="tree-name">${escapeHtml(node.name)}</span>
        `;

        if (node.is_folder) {
            const children = document.createElement('div');
            children.classList.add('tree-children');
            if (node.children && node.children.length > 0) {
                renderFileTree(node.children, children, depth + 1);
            }

            item.onclick = (e) => {
                e.stopPropagation();
                const isExpanded = children.classList.toggle('expanded');
                const arrow = item.querySelector('.tree-icon');
                arrow.innerHTML = `${isExpanded ? '▼' : '▶'} 📁`;
            };

            container.appendChild(item);
            container.appendChild(children);
        } else {
            item.onclick = () => previewFile(node.path, node.name);
            container.appendChild(item);
        }
    });
}

function getFileIcon(name) {
    const ext = name.split('.').pop().toLowerCase();
    const icons = {
        md: '📝', json: '📋', py: '🐍', js: '📜', ts: '📜',
        html: '🌐', css: '🎨', txt: '📄', yaml: '⚙️', yml: '⚙️',
        zip: '📦', png: '🖼️', jpg: '🖼️', svg: '🖼️',
    };
    return icons[ext] || '📄';
}

async function previewFile(path, name) {
    try {
        const resp = await fetch(`${API_BASE}/api/files/content/${path}`);
        const text = await resp.text();
        const ext = name.split('.').pop().toLowerCase();

        els.filePreviewTitle.textContent = name;
        state._previewPath = path;

        if (ext === 'md') {
            els.filePreviewBody.innerHTML = `<div class="markdown-content">${renderMarkdown(text)}</div>`;
        } else {
            const pre = document.createElement('pre');
            const code = document.createElement('code');
            code.textContent = text;
            if (['py', 'js', 'ts', 'json', 'yaml', 'yml', 'html', 'css'].includes(ext)) {
                code.classList.add(`language-${ext === 'yml' ? 'yaml' : ext}`);
            }
            pre.appendChild(code);
            els.filePreviewBody.innerHTML = '';
            els.filePreviewBody.appendChild(pre);
            highlightCode(els.filePreviewBody);
        }

        els.filePreviewModal.classList.remove('hidden');
    } catch (e) {
        console.error('Failed to preview file:', e);
    }
}

function downloadFile() {
    if (state._previewPath) {
        window.open(`${API_BASE}/api/files/download/${state._previewPath}`, '_blank');
    }
}

// ========== Sidebar Tabs ==========
function initSidebarTabs() {
    $$('.sidebar-tab').forEach((tab) => {
        tab.addEventListener('click', () => {
            $$('.sidebar-tab').forEach((t) => t.classList.remove('active'));
            $$('.sidebar-panel').forEach((p) => p.classList.remove('active'));
            tab.classList.add('active');
            $(`#${tab.dataset.tab}`).classList.add('active');
        });
    });
}

// ========== Event Listeners ==========
function initEventListeners() {
    // Sidebar toggle
    els.sidebarToggle.addEventListener('click', () => {
        els.sidebar.classList.toggle('collapsed');
    });

    // Send message
    els.sendBtn.addEventListener('click', sendMessage);
    els.chatInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // Auto-resize textarea
    els.chatInput.addEventListener('input', () => {
        els.chatInput.style.height = 'auto';
        els.chatInput.style.height = Math.min(els.chatInput.scrollHeight, 150) + 'px';
    });

    // Voice
    els.voiceBtn.addEventListener('click', toggleVoice);

    // Agent select
    els.agentSelect.addEventListener('change', updateAgentHint);

    // New chat
    els.newChatBtn.addEventListener('click', newChat);

    // New agent/skill
    els.newAgentBtn.addEventListener('click', () => openEditor('create-agent'));
    els.newSkillBtn.addEventListener('click', () => openEditor('create-skill'));

    // Editor modal
    els.editorSave.addEventListener('click', saveEditor);
    els.editorCancel.addEventListener('click', closeEditor);
    els.editorClose.addEventListener('click', closeEditor);

    // File preview modal
    els.filePreviewClose.addEventListener('click', () => els.filePreviewModal.classList.add('hidden'));
    els.fileDownloadBtn.addEventListener('click', downloadFile);

    // Refresh files
    els.refreshFilesBtn.addEventListener('click', loadFileTree);

    // Close modals on click outside
    els.editorModal.addEventListener('click', (e) => {
        if (e.target === els.editorModal) closeEditor();
    });
    els.filePreviewModal.addEventListener('click', (e) => {
        if (e.target === els.filePreviewModal) els.filePreviewModal.classList.add('hidden');
    });
}

// ========== Init ==========
async function init() {
    initSidebarTabs();
    initEventListeners();
    initVoice();
    showEmptyState();
    connectWebSocket();

    // Load data
    await Promise.all([loadAgents(), loadSkills(), loadSessions(), loadFileTree()]);
}

document.addEventListener('DOMContentLoaded', init);
