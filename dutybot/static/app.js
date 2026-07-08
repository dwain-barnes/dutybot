const API = '';
let currentConversationId = null;
let memoryPanelOpen = false;
let isSending = false;

// DOM elements
const messagesContainer = document.getElementById('messages-container');
const welcomeScreen = document.getElementById('welcome-screen');
const chatMessages = document.getElementById('chat-messages');
const messageInput = document.getElementById('message-input');
const sendBtn = document.getElementById('send-btn');
const conversationsList = document.getElementById('conversations-list');
const typingIndicator = document.getElementById('typing-indicator');
const chatTitle = document.getElementById('chat-title');
const memoryPanel = document.getElementById('memory-panel');
const memoryList = document.getElementById('memory-list');
const memoryCount = document.getElementById('memory-count');
const mobileOverlay = document.getElementById('mobile-overlay');
const sidebar = document.getElementById('sidebar');

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    loadConversations();
    loadMemory();
    autoResizeTextarea();
});

// Auto-resize textarea
function autoResizeTextarea() {
    messageInput.addEventListener('input', () => {
        messageInput.style.height = 'auto';
        messageInput.style.height = Math.min(messageInput.scrollHeight, 160) + 'px';
    });
}

// Keyboard shortcut
messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

// Send message
async function sendMessage(prefill = null) {
    const text = prefill || messageInput.value.trim();
    // isSending also guards the Enter-key path, which bypasses the disabled button
    if (!text || isSending) return;
    isSending = true;

    messageInput.value = '';
    messageInput.style.height = 'auto';
    sendBtn.disabled = true;

    // Show chat area, hide welcome
    showChatArea();

    // Add user message to UI
    appendMessage('user', text);

    // Show typing indicator
    typingIndicator.classList.add('visible');
    scrollToBottom();

    try {
        const resp = await fetch(`${API}/api/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                conversation_id: currentConversationId,
                message: text,
            }),
        });
        const data = await resp.json();

        if (data.error) {
            // The user message was still saved server-side — adopt the conversation
            // so a retry continues it instead of creating an orphan.
            if (!currentConversationId && data.conversation_id) {
                currentConversationId = data.conversation_id;
                loadConversations();
            }
            appendMessage('assistant', `Error: ${data.error}`);
        } else {
            // Update conversation ID if new
            if (!currentConversationId) {
                currentConversationId = data.conversation_id;
                loadConversations();
            }
            appendMessage('assistant', data.message.content, data.verification);
        }

        // Refresh memory
        loadMemory();
    } catch (err) {
        appendMessage('assistant', `Connection error: ${err.message}. Is the server running?`);
    } finally {
        isSending = false;
        typingIndicator.classList.remove('visible');
        sendBtn.disabled = false;
        messageInput.focus();
    }
}

// Append message to chat
function appendMessage(role, content, verification = null) {
    const div = document.createElement('div');
    div.className = `message ${role}`;

    const avatar = role === 'user' ? 'You' : 'DB';
    let verifyHtml = '';

    if (verification && role === 'assistant') {
        if (verification.status === 'found' && verification.sources && verification.sources.length > 0) {
            const sourcesHtml = verification.sources.map(s => {
                const snippet = s.snippet || '';
                const shortSnippet = snippet.length > 150 ? snippet.substring(0, 150) + '...' : snippet;
                return `<a href="${escapeHtml(safeUrl(s.url))}" target="_blank" rel="noopener">${escapeHtml(s.title)}</a>
                 <span class="verify-snippet">${escapeHtml(shortSnippet)}</span>`;
            }).join('');

            const cc = verification.citation_check;
            const ccHtml = (cc && cc.unmatched && cc.unmatched.length > 0)
                ? `<div class="citation-warn">Cited but not found in the retrieved sources: ${escapeHtml(cc.unmatched.join('; '))} — verify before relying on these.</div>`
                : '';

            verifyHtml = `
                <div class="verify-panel">
                    <div class="verify-header" onclick="this.parentElement.classList.toggle('open')">
                        <span class="verify-badge">Sources</span>
                        <span class="verify-text">${verification.count} source${verification.count !== 1 ? 's' : ''} found on legislation.gov.uk</span>
                        <span class="verify-chevron"></span>
                    </div>
                    <div class="verify-sources">${sourcesHtml}</div>
                    ${ccHtml}
                </div>
            `;
        } else if (verification.status === 'none' || verification.status === 'failed') {
            const reason = verification.status === 'failed'
                ? 'the legislation.gov.uk lookup failed'
                : 'no matching legislation.gov.uk sources were found';
            verifyHtml = `
                <div class="verify-panel warn">
                    <div class="verify-header">
                        <span class="verify-badge warn">Not verified</span>
                        <span class="verify-text">This answer was not checked against legislation.gov.uk — ${reason}. Verify statutory details against official sources.</span>
                    </div>
                </div>
            `;
        }
    }

    div.innerHTML = `
        <div class="message-avatar">${avatar}</div>
        <div class="message-content">
            <div class="message-bubble">${formatContent(content)}</div>
            ${verifyHtml}
        </div>
    `;

    chatMessages.appendChild(div);
    scrollToBottom();
}

// Basic markdown-like formatting
function formatContent(text) {
    // Escape HTML
    text = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    // Code blocks
    text = text.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
    // Inline code
    text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Bold
    text = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // Italic
    text = text.replace(/\*(.+?)\*/g, '<em>$1</em>');
    // Line breaks
    text = text.replace(/\n/g, '<br>');

    return text;
}

function scrollToBottom() {
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

function showChatArea() {
    welcomeScreen.style.display = 'none';
    chatMessages.style.display = 'flex';
}

function showWelcome() {
    welcomeScreen.style.display = 'flex';
    chatMessages.style.display = 'none';
}

// Conversations
async function loadConversations() {
    try {
        const resp = await fetch(`${API}/api/conversations`);
        const convs = await resp.json();

        conversationsList.innerHTML = '';
        convs.forEach(conv => {
            const div = document.createElement('div');
            div.className = `conv-item${conv.id === currentConversationId ? ' active' : ''}`;
            div.innerHTML = `
                <span class="title">${escapeHtml(conv.title)}</span>
                <button class="delete-btn">&times;</button>
            `;
            div.addEventListener('click', () => loadConversation(conv.id, conv.title));
            div.querySelector('.delete-btn').addEventListener('click', (event) => {
                event.stopPropagation();
                deleteConversation(conv.id);
            });
            conversationsList.appendChild(div);
        });
    } catch (err) {
        console.error('Failed to load conversations:', err);
    }
}

async function loadConversation(convId, title) {
    currentConversationId = convId;
    chatTitle.textContent = title || 'Chat';
    showChatArea();
    chatMessages.innerHTML = '';
    closeSidebar();

    try {
        const resp = await fetch(`${API}/api/conversations/${convId}/messages`);
        const messages = await resp.json();

        messages.forEach(msg => {
            appendMessage(msg.role, msg.content);
        });

        // Highlight active in sidebar
        document.querySelectorAll('.conv-item').forEach(el => el.classList.remove('active'));
        document.querySelectorAll('.conv-item').forEach(el => {
            if (el.querySelector('.title').textContent === title) {
                el.classList.add('active');
            }
        });
    } catch (err) {
        appendMessage('assistant', `Failed to load conversation: ${err.message}`);
    }
}

async function deleteConversation(convId) {
    try {
        await fetch(`${API}/api/conversations/${convId}`, { method: 'DELETE' });
        if (currentConversationId === convId) {
            currentConversationId = null;
            chatTitle.textContent = 'DutyBot';
            showWelcome();
        }
        loadConversations();
    } catch (err) {
        console.error('Failed to delete conversation:', err);
    }
}

function newChat() {
    currentConversationId = null;
    chatTitle.textContent = 'DutyBot';
    chatMessages.innerHTML = '';
    showWelcome();
    closeSidebar();
    document.querySelectorAll('.conv-item').forEach(el => el.classList.remove('active'));
}

// Memory
async function loadMemory() {
    try {
        const resp = await fetch(`${API}/api/memory`);
        const memories = await resp.json();

        memoryCount.textContent = memories.length;

        if (memories.length === 0) {
            memoryList.innerHTML = '<div class="memory-empty">No memories yet. DutyBot will remember key facts as you chat.</div>';
        } else {
            // No inline onclick here: memory keys are model-generated text, so they
            // must never be spliced into executable attributes.
            memoryList.innerHTML = memories.map(m => `
                <div class="memory-item">
                    <span class="key">${escapeHtml(m.key)}</span>
                    <span class="value">${escapeHtml(m.value)}</span>
                    <button class="remove-btn" data-key="${escapeHtml(m.key)}">&times;</button>
                </div>
            `).join('');
            memoryList.querySelectorAll('.remove-btn').forEach(btn => {
                btn.addEventListener('click', () => deleteMemory(btn.dataset.key));
            });
        }
    } catch (err) {
        console.error('Failed to load memory:', err);
    }
}

async function deleteMemory(key) {
    try {
        await fetch(`${API}/api/memory/${encodeURIComponent(key)}`, { method: 'DELETE' });
        loadMemory();
    } catch (err) {
        console.error('Failed to delete memory:', err);
    }
}

async function clearMemory() {
    try {
        await fetch(`${API}/api/memory`, { method: 'DELETE' });
        loadMemory();
    } catch (err) {
        console.error('Failed to clear memory:', err);
    }
}

function toggleMemoryPanel() {
    memoryPanelOpen = !memoryPanelOpen;
    memoryPanel.classList.toggle('visible', memoryPanelOpen);
    if (memoryPanelOpen) loadMemory();
}

// Mobile sidebar
function toggleSidebar() {
    sidebar.classList.toggle('open');
    mobileOverlay.classList.toggle('visible');
}

function closeSidebar() {
    sidebar.classList.remove('open');
    mobileOverlay.classList.remove('visible');
}

// Welcome card click
function askQuestion(text) {
    messageInput.value = text;
    sendMessage(text);
}

// Utility — escapes quotes too, so output is safe inside HTML attributes
function escapeHtml(text) {
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// Only allow http(s) links — search results could carry javascript: URLs
function safeUrl(url) {
    try {
        const parsed = new URL(url);
        if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
            return parsed.href;
        }
    } catch (err) {
        // fall through
    }
    return '#';
}
