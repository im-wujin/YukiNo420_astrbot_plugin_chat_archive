let API_KEY = '';
localStorage.removeItem('astr_chat_key');
let currentPage = 1;
const limit = 50;
let nextCursor = 0;
let activeSessionId = '';
let activeUserId = '';
let isHistoryLoading = false;
const avatarPreloadCache = new Map();
const avatarResolvedCache = new Map();

window.copyToClipboard = (text) => {
    if (!text) return;
    navigator.clipboard.writeText(text).then(() => {
        const toast = document.createElement('div');
        toast.style = "position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:rgba(15,23,42,0.85); backdrop-filter:blur(8px); border:1px solid rgba(255,255,255,0.1); box-shadow:0 10px 30px rgba(0,0,0,0.5); color:white; padding:10px 20px; border-radius:100px; z-index:9999; font-size:0.85rem; font-weight:500; animation: fadeUp 0.3s cubic-bezier(0.4, 0, 0.2, 1);";
        toast.innerText = "已复制 ID: " + text;
        document.body.appendChild(toast);
        setTimeout(() => {
            toast.style.animation = "fadeDown 0.3s ease";
            setTimeout(() => toast.remove(), 300);
        }, 1500);
    }).catch(err => console.error('Copy failed', err));
};
window.userMap = {};
window.globalTopUsers = [];
const memberPageSize = 10;
const memberInitialPageMax = 30;
const memberAutoFillMaxRequests = 6;
let sidebarMemberUsers = [];
let rankMemberUsers = [];
let memberOffset = 0;
let memberTotal = 0;
let memberHasMore = false;
let rankOffset = 0;
let rankTotal = 0;
let rankHasMore = false;
let memberSearchKeyword = '';
let memberSearchTimer = null;
let memberRequestSeq = 0;
let rankRequestSeq = 0;
let memberAutoFillPending = false;
let memberAutoFillCount = 0;
let filterStart = 0;
let filterEnd = 0;
let activeMsgType = '';

function isFriendSessionType(type = activeMsgType) {
    return safeText(type).toLowerCase().includes('friend');
}

function getInitialMemberLimit() {
    const height = window.innerHeight || document.documentElement.clientHeight || 900;
    const estimated = Math.ceil((height - 180) / 74);
    return Math.max(memberPageSize, Math.min(memberInitialPageMax, estimated));
}


async function fetchAPI(endpoint, method = 'GET', body = null) {
    const headers = {
        'Content-Type': 'application/json'
    };
    if (API_KEY) headers['X-API-Key'] = API_KEY;
    const options = { method, headers };
    if (body) options.body = JSON.stringify(body);

    try {
        const response = await fetch(endpoint, options);
        if (response.status === 401) {
            showAuth(true);
            throw new Error('Unauthorized');
        }
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return await response.json();
    } catch (err) {
        if (err.message === 'Unauthorized') {
            console.error('API Key invalid or expired');
        } else {
            console.error('Fetch error:', err);
            const toast = document.createElement('div');
            toast.style = "position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:rgba(239,68,68,0.9); backdrop-filter:blur(8px); border:1px solid rgba(255,255,255,0.1); box-shadow:0 10px 30px rgba(0,0,0,0.5); color:white; padding:10px 20px; border-radius:100px; z-index:9999; font-size:0.85rem; font-weight:500; animation: fadeUp 0.3s cubic-bezier(0.4, 0, 0.2, 1);";
            toast.innerText = "网络请求失败，请检查连接或稍后重试";
            document.body.appendChild(toast);
            setTimeout(() => {
                toast.style.animation = "fadeDown 0.3s ease";
                setTimeout(() => toast.remove(), 300);
            }, 3000);
        }
        throw err;
    }
}

function showAuth(show) {
    const overlay = document.getElementById('auth-overlay');
    if (show) overlay.classList.remove('hidden');
    else overlay.classList.add('hidden');
}

async function ensureAuthCookie() {
    if (!API_KEY) return false;
    try {
        const res = await fetch('/api/auth/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: API_KEY })
        });
        if (res.status === 401) {
            showAuth(true);
            return false;
        }
        return res.ok;
    } catch (e) {
        console.warn('Auth cookie refresh failed', e);
        return false;
    }
}

async function verifyLogin() {
    const key = document.getElementById('api-key-input').value;
    try {
        const res = await fetch('/api/auth/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: key })
        });
        const data = await res.json();
        if (data.success) {
            API_KEY = key;
            showAuth(false);
            initApp();
        } else {
            document.getElementById('auth-error').style.display = 'block';
        }
    } catch (e) {
        console.error(e);
    }
}

async function logout() {
    try {
        await fetch('/api/auth/logout', { method: 'POST', headers: { 'X-API-Key': API_KEY } });
    } catch (e) {
        console.warn('Logout request failed', e);
    }
    API_KEY = '';
    location.reload();
}

const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false
});

function formatTime(ts) {
    return dateFormatter.format(new Date(ts * 1000));
}

function getDateStr(ts) {
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

function escapeAttr(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function safeText(value, fallback = '') {
    if (value === null || value === undefined) return fallback;
    return String(value);
}

function safeCount(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
}

function getImageDisplayStyle(width, height) {
    if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0 || width > 100000 || height > 100000) {
        return '';
    }

    const ratio = width / height;
    let maxWidth = 520;
    let maxHeight = 420;

    if (ratio >= 2.4) {
        maxWidth = 560;
        maxHeight = 240;
    } else if (ratio >= 1.25) {
        maxWidth = 540;
        maxHeight = 380;
    } else if (ratio <= 0.45) {
        maxWidth = 300;
        maxHeight = 520;
    } else if (ratio <= 0.8) {
        maxWidth = 360;
        maxHeight = 500;
    } else {
        maxWidth = 440;
        maxHeight = 440;
    }

    let scale = Math.min(maxWidth / width, maxHeight / height, 1);
    const longEdge = Math.max(width, height);
    if (longEdge < 180) {
        scale = Math.min(maxWidth / width, maxHeight / height, 180 / longEdge, 2.5);
    }

    const displayWidth = Math.max(48, Math.round(width * scale));
    return ` style="width:${displayWidth}px;max-width:100%;aspect-ratio:${width}/${height};"`;
}

function formatSessionPreview(text) {
    if (!text) return '';
    const rawText = String(text);
    const previewText = isShareJsonPayload(rawText.trim())
        ? formatSharePreview(rawText.trim())
        : replaceCqJsonCodes(rawText, data => formatSharePreview(data));
    return previewText
        .replace(/\[CQ:image,[^\]]*\]/g, '[图片]')
        .replace(/\[CQ:video,[^\]]*\]/g, '[视频]')
        .replace(/\[CQ:record,[^\]]*\]/g, '[语音]')
        .replace(/\[CQ:face,[^\]]*\]/g, '[表情]')
        .replace(/\[CQ:at,qq=all[^\]]*\]/g, '@全体成员')
        .replace(/\[CQ:at,qq=([^\],]+)[^\]]*\]/g, '@$1')
        .replace(/\[CQ:reply,[^\]]*\]/g, '[回复]')
        .replace(/\s+/g, ' ')
        .trim();
}

function findCqJsonEnd(text, dataStart) {
    let braceDepth = 0;
    let inString = false;
    let escaped = false;
    let started = false;

    for (let i = dataStart; i < text.length; i++) {
        const ch = text[i];

        if (!started) {
            if (/\s/.test(ch)) continue;
            if (ch !== '{') return -1;
            started = true;
        }

        if (inString) {
            if (escaped) {
                escaped = false;
            } else if (ch === '\\') {
                escaped = true;
            } else if (ch === '"') {
                inString = false;
            }
            continue;
        }

        if (ch === '"') {
            inString = true;
        } else if (ch === '{') {
            braceDepth++;
        } else if (ch === '}') {
            braceDepth--;
            if (braceDepth === 0) {
                let closeIndex = i + 1;
                while (closeIndex < text.length && /\s/.test(text[closeIndex])) closeIndex++;
                return text[closeIndex] === ']' ? closeIndex : -1;
            }
        }
    }

    return -1;
}

function replaceCqJsonCodes(text, replacer) {
    const prefix = '[CQ:json,data=';
    let output = '';
    let cursor = 0;

    while (cursor < text.length) {
        const start = text.indexOf(prefix, cursor);
        if (start === -1) {
            output += text.slice(cursor);
            break;
        }

        const dataStart = start + prefix.length;
        let end = findCqJsonEnd(text, dataStart);
        let data = end === -1 ? '' : text.slice(dataStart, end);

        if (end === -1 || !parseCqJsonData(data)) {
            for (let probe = text.indexOf(']', dataStart); probe !== -1; probe = text.indexOf(']', probe + 1)) {
                const candidate = text.slice(dataStart, probe);
                if (parseCqJsonData(candidate)) {
                    end = probe;
                    data = candidate;
                    break;
                }
            }
        }

        if (end === -1) {
            output += text.slice(cursor);
            break;
        }

        output += text.slice(cursor, start);
        output += replacer(data);
        cursor = end + 1;
    }

    return output;
}

function decodeCqJsonData(data) {
    if (!data) return '';
    let decoded = String(data);
    const entities = {
        '&quot;': '"',
        '&#34;': '"',
        '&#39;': "'",
        '&apos;': "'",
        '&#44;': ',',
        '&#91;': '[',
        '&#93;': ']',
        '&lt;': '<',
        '&gt;': '>',
        '&amp;': '&'
    };

    for (let i = 0; i < 3; i++) {
        const next = decoded.replace(/&(quot|apos|lt|gt|amp);|&#(34|39|44|91|93);/g, match => entities[match] || match);
        if (next === decoded) break;
        decoded = next;
    }
    return decoded;
}

function parseCqJsonData(data) {
    try {
        return JSON.parse(decodeCqJsonData(data));
    } catch (err) {
        return null;
    }
}

function decodeCqParamValue(value) {
    if (!value) return '';
    return String(value)
        .replace(/&amp;/g, '&')
        .replace(/&#44;/g, ',')
        .replace(/&#91;/g, '[')
        .replace(/&#93;/g, ']');
}

function getJsonFieldFromText(text, field) {
    if (!text) return '';
    const reg = new RegExp(`"${field}"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"`);
    const match = String(text).match(reg);
    if (!match) return '';
    return match[1]
        .replace(/\\"/g, '"')
        .replace(/\\\\\//g, '/')
        .replace(/\\\\n/g, '\n')
        .trim();
}

function firstText(...values) {
    for (const value of values) {
        if (typeof value === 'string' && value.trim()) return value.trim();
        if (typeof value === 'number' && Number.isFinite(value)) return String(value);
    }
    return '';
}

function eachShareMeta(payload, callback) {
    if (!payload || typeof payload !== 'object' || !payload.meta || typeof payload.meta !== 'object') return;
    Object.values(payload.meta).forEach(item => {
        if (item && typeof item === 'object') callback(item);
    });
}

function detectSharePlatformFromUrl(url) {
    if (!url) return '';
    const value = String(url).toLowerCase();
    const patterns = [
        [/bilibili\.com|b23\.tv|bili2233\.cn/, '哔哩哔哩'],
        [/music\.163\.com|y\.music\.163\.com/, '网易云音乐'],
        [/y\.qq\.com|c6\.y\.qq\.com|i\.y\.qq\.com/, 'QQ音乐'],
        [/douyin\.com|iesdouyin\.com/, '抖音'],
        [/kuaishou\.com|gifshow\.com/, '快手'],
        [/xiaohongshu\.com|xhslink\.com/, '小红书'],
        [/weibo\.com|weibo\.cn/, '微博'],
        [/zhihu\.com/, '知乎'],
        [/github\.com/, 'GitHub'],
        [/mp\.weixin\.qq\.com/, '微信文章'],
        [/acfun\.cn/, 'AcFun']
    ];
    const match = patterns.find(([reg]) => reg.test(value));
    return match ? match[1] : '';
}

function getCqJsonShareInfo(data) {
    const payload = parseCqJsonData(data);
    const decodedData = decodeCqJsonData(data);
    if (!payload) {
        const tag = getJsonFieldFromText(decodedData, 'tag');
        const source = getJsonFieldFromText(decodedData, 'source');
        const sourceName = getJsonFieldFromText(decodedData, 'source_name');
        const metaTitle = getJsonFieldFromText(decodedData, 'title');
        const metaDesc = getJsonFieldFromText(decodedData, 'desc');
        const prompt = getJsonFieldFromText(decodedData, 'prompt');
        const urls = ['qqdocurl', 'jumpUrl', 'url', 'source_url'].map(key => getJsonFieldFromText(decodedData, key));
        const platform = firstText(tag, source, sourceName, urls.map(detectSharePlatformFromUrl).find(Boolean), metaTitle, prompt.match(/^\[([^\]]+)\]/)?.[1], '链接分享');
        const title = (metaTitle && metaTitle !== platform ? metaTitle : metaDesc) || prompt.replace(/^\[[^\]]+\]\s*/, '');
        return { platform, title };
    }

    let platform = '';
    let title = '';
    let metaTitle = '';
    let metaDesc = '';
    const urls = [];

    eachShareMeta(payload, item => {
        platform = platform || firstText(item.tag, item.source, item.source_name);
        metaTitle = metaTitle || firstText(item.title);
        metaDesc = metaDesc || firstText(item.desc);
        ['qqdocurl', 'jumpUrl', 'url', 'source_url', 'preview'].forEach(key => {
            if (item[key]) urls.push(item[key]);
        });
    });

    const promptPlatform = firstText(payload.prompt).match(/^\[([^\]]+)\]/)?.[1] || '';
    platform = platform || urls.map(detectSharePlatformFromUrl).find(Boolean) || metaTitle || promptPlatform || firstText(payload.app);
    title = (metaTitle && metaTitle !== platform ? metaTitle : metaDesc) || firstText(payload.prompt).replace(/^\[[^\]]+\]\s*/, '') || firstText(payload.desc);

    return {
        platform: platform || '链接分享',
        title
    };
}

function formatSharePreview(data) {
    const info = getCqJsonShareInfo(data);
    return info.title ? `[${info.platform}] ${info.title}` : `[${info.platform}]`;
}

function isShareJsonPayload(text) {
    const payload = parseCqJsonData(text);
    return Boolean(payload && typeof payload === 'object' && payload.meta && payload.app);
}

function debounceMemberSearch(value) {
    memberSearchKeyword = safeText(value).trim();
    clearTimeout(memberSearchTimer);
    memberSearchTimer = setTimeout(() => {
        fetchMembers({ target: 'sidebar', keyword: memberSearchKeyword, offset: 0, append: false });
    }, 180);
}

function toggleCategory(header, content) {
    const willCollapse = !header.classList.contains('collapsed');
    header.classList.toggle('collapsed', willCollapse);

    if (willCollapse) {
        content.style.maxHeight = `${content.scrollHeight}px`;
        content.offsetHeight;
        content.classList.add('hidden');
    } else {
        content.style.maxHeight = '0px';
        content.classList.remove('hidden');
        content.offsetHeight;
        content.style.maxHeight = `${content.scrollHeight}px`;
        const clearHeight = () => {
            if (!content.classList.contains('hidden')) content.style.maxHeight = '';
            content.removeEventListener('transitionend', clearHeight);
        };
        content.addEventListener('transitionend', clearHeight);
    }
}

function formatMsg(text) {
    if (!text) return "";

    if (text.startsWith("<Event,") || (typeof text === 'string' && text.includes("'raw_message':"))) {
        const match = text.match(/['"]raw_message['"]\s*:\s*['"](.*?)['"]/);
        if (match && match[1]) {
            text = match[1];
        } else {
            return `<span style="color: var(--text-muted); font-size: 0.8rem; font-style: italic;">[无法解析的消息内容]</span>`;
        }
    }

    const shareCards = [];
    const makeSharePlaceholder = data => {
        const info = getCqJsonShareInfo(data);
        const safePlatform = escapeAttr(info.platform);
        const safeTitle = escapeAttr(info.title);
        const titleHtml = safeTitle ? `<span class="msg-share-title">${safeTitle}</span>` : '';
        const index = shareCards.length;
        shareCards.push(`<span class="msg-share-card"><span class="msg-share-platform">🔗 ${safePlatform}</span>${titleHtml}</span>`);
        return `__CQ_JSON_SHARE_${index}__`;
    };

    const textWithSharePlaceholders = isShareJsonPayload(String(text).trim())
        ? makeSharePlaceholder(String(text).trim())
        : replaceCqJsonCodes(text, makeSharePlaceholder);

    const div = document.createElement('div');
    div.innerText = textWithSharePlaceholders;
    let escaped = div.innerHTML;

    function isSafeUrl(url) {
        if (!url) return false;
        return /^(https?:\/\/|\/static\/)/i.test(url);
    }

    // Helper: route NTQQ/gchat media URLs through backend proxy (with auth)
    function proxyUrl(url) {
        const proxyDomains = ['multimedia.nt.qq.com.cn', 'gchat.qpic.cn'];
        if (proxyDomains.some(d => url.includes(d))) {
            return `/api/proxy/image?url=${encodeURIComponent(url)}`;
        }
        return url;
    }

    // CQ Code Handling
    shareCards.forEach((html, index) => {
        escaped = escaped.replace(`__CQ_JSON_SHARE_${index}__`, html);
    });

    // Images
    escaped = escaped.replace(/\[CQ:image,([^\]]+)\]/g, (match, inner) => {
        const urlMatch = inner.match(/url=([^,\]]+)/);
        if (urlMatch && urlMatch[1]) {
            let url = decodeCqParamValue(urlMatch[1]);
            url = proxyUrl(url);
            if (!isSafeUrl(url)) return `<span class="msg-tag">🖼️ [图片]</span>`;
            const safeUrl = escapeAttr(url);
            const widthMatch = inner.match(/(?:^|,)width=(\d+)(?:,|$)/);
            const heightMatch = inner.match(/(?:^|,)height=(\d+)(?:,|$)/);
            const width = widthMatch ? parseInt(widthMatch[1], 10) : 0;
            const height = heightMatch ? parseInt(heightMatch[1], 10) : 0;
            const sizeStyle = getImageDisplayStyle(width, height);
            return `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer"><img src="${safeUrl}" class="msg-image" alt="图片" loading="lazy"${sizeStyle} onload="this.classList.add('loaded')" onerror="this.parentElement.outerHTML='<span class=\\'msg-tag\\' style=\\'opacity:0.6;\\'>🖼️ [图片]</span>'" /></a>`;
        }
        return `<span class="msg-tag">🖼️ [图片]</span>`;
    });

    // QQ Faces
    escaped = escaped.replace(/\[CQ:face,id=(\d+)[^\]]*\]/g, (match, id) => {
        return `<img src="https://gxh.vip.qq.com/sys/hycdn/sng/face/s/${id}.png" class="msg-face" alt="表情" loading="lazy" onload="this.style.background='none'" onerror="this.style.display='none'" />`;
    });
    escaped = escaped.replace(/\[CQ:face,[^\]]*\]/g, '<span class="msg-tag">😊 表情</span>');

    // Video Handling
    escaped = escaped.replace(/\[CQ:video,([^\]]+)\]/g, (match, inner) => {
        const urlMatch = inner.match(/url=([^,\]]+)/);
        if (urlMatch && urlMatch[1]) {
            let url = decodeCqParamValue(urlMatch[1]);
            url = proxyUrl(url);
            if (!isSafeUrl(url)) return `<span class="msg-tag">🎬 [视频]</span>`;
            const safeUrl = escapeAttr(url);
            return `<video src="${safeUrl}" controls class="msg-video" preload="metadata" onerror="this.outerHTML='<span class=\\'msg-tag\\' style=\\'opacity:0.6;\\'>🎬 [视频加载失败]</span>'"></video>`;
        }
        return `<span class="msg-tag">🎬 [视频]</span>`;
    });

    // Voice/Record Handling
    escaped = escaped.replace(/\[CQ:record,([^\]]+)\]/g, (match, inner) => {
        const urlMatch = inner.match(/url=([^,\]]+)/);
        if (urlMatch && urlMatch[1]) {
            let url = decodeCqParamValue(urlMatch[1]);
            url = proxyUrl(url);
            if (!isSafeUrl(url)) return `<span class="msg-tag">🎙️ [语音]</span>`;
            const safeUrl = escapeAttr(url);
            return `<div class="msg-audio-wrap"><span class="msg-tag" style="margin-right:6px;">🎙️</span><audio src="${safeUrl}" controls preload="metadata" class="msg-audio" onerror="this.parentElement.outerHTML='<span class=\\'msg-tag\\' style=\\'opacity:0.6;\\'>🎙️ [语音]</span>'"></audio></div>`;
        }
        return `<span class="msg-tag">🎙️ [语音]</span>`;
    });

    const tags = ["动画表情", "文件", "红包"];
    tags.forEach(tag => {
        const regex = new RegExp(`\\[${tag}\\]`, 'g');
        escaped = escaped.replace(regex, `<span class="msg-tag">📄 [${tag}]</span>`);
    });
    // Fallback plain text tags for voice/video without CQ codes
    escaped = escaped.replace(/\[语音\]/g, '<span class="msg-tag">🎙️ [语音]</span>');
    escaped = escaped.replace(/\[视频\]/g, '<span class="msg-tag">🎥 [视频]</span>');

    escaped = escaped.replace(/\[CQ:at,qq=all[^\]]*\]/g, '<span class="msg-tag" style="background: rgba(239, 68, 68, 0.2); border-color: rgba(239, 68, 68, 0.4); color: #fca5a5;">@全体成员</span>');
    escaped = escaped.replace(/\[CQ:at,qq=(\d+)[^\]]*\]/g, (match, qq) => {
        let name = window.userMap && window.userMap[qq] ? window.userMap[qq] : qq;
        const safeName = escapeAttr(name);
        return `<span class="msg-tag" style="padding:2px 8px; gap:4px; display:inline-flex; align-items:center; color:var(--text-main); background:rgba(255,255,255,0.1);">
            <img src="${getAvatarUrl(qq)}" onerror="this.src=getAvatarUrl('fallback')" style="width:16px; height:16px; border-radius:50%; object-fit:cover;" /> 
            @${safeName}
        </span>`;
    });
    escaped = escaped.replace(/\[CQ:reply,[^\]]*\]/g, '<span class="msg-tag" style="opacity: 0.8; background:transparent; border-color:rgba(255,255,255,0.2);">💬 回复</span>');

    // Recall Links
    escaped = escaped.replace(/🛡️ \[撤回了一条消息 \(ID: ([^\]]+)\)\]/g, (match, id) => {
        const safeId = escapeAttr(id);
        return `🛡️ [撤回了一条消息 (ID: <span class="recall-link" data-msg-id="${safeId}">${safeId}</span>)]`;
    });

    return escaped;
}

function settleLoadedImages(container) {
    container.querySelectorAll('img.msg-image').forEach(img => {
        if (img.complete && img.naturalWidth > 0) {
            img.classList.add('loaded');
        }
    });
}

window.scrollToMsg = (msgId) => {
    const safeMsgId = CSS.escape(msgId);
    const el = document.querySelector(`.msg-bubble[data-msg-id="${safeMsgId}"]`);
    if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.classList.add('highlight-flash');
        setTimeout(() => el.classList.remove('highlight-flash'), 2000);
    } else {
        // Try searching in the entire document just in case
        const altEl = document.querySelector(`[data-msg-id="${safeMsgId}"]`);
        if (altEl) {
            altEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
            altEl.classList.add('highlight-flash');
            setTimeout(() => altEl.classList.remove('highlight-flash'), 2000);
        } else {
            console.warn(`Message ${msgId} not found in DOM.`);
            // Show a toast or notification if we had a library, but alert is fine for now
            const toast = document.createElement('div');
            toast.style = "position:fixed; top:20px; left:50%; transform:translateX(-50%); background:rgba(0,0,0,0.8); color:white; padding:10px 20px; border-radius:20px; z-index:9999; font-size:0.9rem; animation: fadeUp 0.3s ease;";
            toast.innerText = "该消息不在当前加载范围内";
            document.body.appendChild(toast);
            setTimeout(() => {
                toast.style.animation = "fadeDown 0.3s ease";
                setTimeout(() => toast.remove(), 300);
            }, 2000);
        }
    }
};

document.addEventListener('click', (event) => {
    const recall = event.target.closest('.recall-link[data-msg-id]');
    if (recall) {
        scrollToMsg(recall.getAttribute('data-msg-id') || '');
        return;
    }

    const copy = event.target.closest('.msg-id[data-copy-id]');
    if (copy) {
        copyToClipboard(copy.getAttribute('data-copy-id') || '');
    }
});

function getAvatarUrl(userId) {
    if (/^\d+$/.test(userId)) {
        return `https://q1.qlogo.cn/g?b=qq&nk=${userId}&s=100`;
    }
    return `data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%2364748b'%3E%3Cpath d='M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 3c1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3 1.34-3 3-3zm0 14.2c-2.5 0-4.71-1.28-6-3.22.03-1.99 4-3.08 6-3.08 1.99 0 5.97 1.09 6 3.08-1.29 1.94-3.5 3.22-6 3.22z'/%3E%3C/svg%3E`;
}

function preloadAvatar(userId) {
    const key = safeText(userId);
    if (avatarPreloadCache.has(key)) return avatarPreloadCache.get(key);

    const url = getAvatarUrl(key);
    const promise = new Promise(resolve => {
        const img = new Image();
        let settled = false;
        const done = (src) => {
            if (settled) return;
            settled = true;
            avatarResolvedCache.set(key, src);
            resolve(src);
        };
        img.onload = () => {
            if (img.decode) img.decode().then(() => done(url)).catch(() => done(url));
            else done(url);
        };
        img.onerror = () => done(getAvatarUrl('fallback'));
        img.src = url;
        setTimeout(() => done(url), 800);
    });
    avatarPreloadCache.set(key, promise);
    return promise;
}

async function preloadUserAvatars(users, limit = 12) {
    const pending = users.slice(0, limit).map(u => preloadAvatar(u.user_id));
    await Promise.allSettled(pending);
}

function showSkeleton(containerId, count = 5) {
    const container = document.getElementById(containerId);
    container.querySelectorAll('.message-group, .skeleton-group, .date-divider, .empty-state').forEach(el => el.remove());
    for (let i = 0; i < count; i++) {
        const sk = document.createElement('div');
        sk.className = 'skeleton-group animate-fade';
        sk.style.animationDelay = `${i * 0.05}s`;
        sk.innerHTML = `
            <div class="avatar-col">
                <div class="skeleton" style="width: 40px; height: 40px; border-radius: 12px;"></div>
            </div>
            <div class="content-col" style="width: 100%;">
                <div class="skeleton" style="width: 100px; height: 16px; margin-bottom: 4px; border-radius: 4px;"></div>
                <div class="skeleton" style="width: 60%; height: 60px; border-radius: 18px;"></div>
            </div>
        `;
        container.appendChild(sk);
    }
}

function createMessageBubble(msg) {
    const isRecalled = msg.is_recalled === 1;
    const bubble = document.createElement('div');
    bubble.className = `msg-bubble animate-fade ${isRecalled ? 'recalled-msg' : ''}`;
    bubble.dataset.msgId = safeText(msg.msg_id);

    const text = document.createElement('div');
    text.className = 'msg-text';
    text.innerHTML = formatMsg(msg.message);
    settleLoadedImages(text);
    if (text.querySelector('.msg-image, .msg-video')) {
        bubble.classList.add('msg-bubble-media');
    }

    const footer = document.createElement('div');
    footer.className = 'msg-footer';

    const id = document.createElement('span');
    id.className = 'msg-id';
    id.title = '平台消息ID';
    id.dataset.copyId = safeText(msg.msg_id);
    id.textContent = `#${safeText(msg.msg_id, 'N/A') || 'N/A'}`;
    footer.appendChild(id);

    if (isRecalled) {
        const recalled = document.createElement('span');
        recalled.className = 'msg-tag';
        recalled.style.background = 'rgba(239, 68, 68, 0.1)';
        recalled.style.color = 'var(--danger)';
        recalled.style.borderColor = 'rgba(239,68,68,0.2)';
        recalled.textContent = '已撤回';
        footer.appendChild(recalled);
    }

    const time = document.createElement('span');
    time.textContent = formatTime(msg.timestamp).split(' ')[1] || '';
    footer.appendChild(time);

    bubble.appendChild(text);
    bubble.appendChild(footer);
    return bubble;
}

async function fetchSessions() {
    try {
        const data = await fetchAPI('/api/sessions');
        if (data.success) {
            const list = document.getElementById('sessionList');
            list.innerHTML = '';

            const groups = {
                'group': { name: '群组会话', items: [] },
                'friend': { name: '个人私聊', items: [] },
                'legacy': { name: '历史归档', items: [] }
            };

            data.data.forEach(s => {
                let category = 'legacy';
                const mt = (s.message_type || '').toLowerCase();
                if (mt.includes('group')) category = 'group';
                else if (mt.includes('friend')) category = 'friend';

                // 强制修正展示名称：如果还是技术 ID，且我们知道是群还是私聊，做个简单的清理
                if (s.name === s.session_id && s.session_id.includes(':')) {
                    const parts = s.session_id.split(':');
                    const id = parts[parts.length - 1];
                    s.name = (category === 'group' ? '群聊: ' : '私聊: ') + id;
                }

                if (groups[category]) groups[category].items.push(s);
            });

            Object.keys(groups).forEach(catKey => {
                const groupData = groups[catKey];
                if (groupData.items.length === 0) return;

                const header = document.createElement('div');
                header.className = 'category-header';
                const label = document.createElement('span');
                label.textContent = `${groupData.name} `;
                const count = document.createElement('small');
                count.style.opacity = '0.5';
                count.style.fontWeight = 'normal';
                count.textContent = groupData.items.length;
                label.appendChild(count);
                const toggle = document.createElement('span');
                toggle.className = 'toggle-icon';
                toggle.textContent = '▼';
                header.append(label, toggle);

                const content = document.createElement('div');
                content.className = 'category-content';

                header.onclick = () => toggleCategory(header, content);

                groupData.items.forEach((s, idx) => {
                    const item = document.createElement('div');
                    item.className = `session-item session-enter ${activeSessionId === s.session_id ? 'active' : ''}`;
                    item.dataset.sessionId = s.session_id;
                    item.style.animationDelay = `${Math.min(idx, 8) * 0.025}s`;
                    item.onclick = (e) => {
                        e.stopPropagation();
                        selectSession(s.session_id, s.name, s.message_type);
                    };

                    const avatarUrl = escapeAttr(s.avatar || getAvatarUrl('fallback'));
                    const lastTime = safeCount(s.last_time);
                    const lastDate = lastTime
                        ? new Date(lastTime * 1000).toLocaleDateString()
                        : '';
                    item.innerHTML = `
                        <img class="session-avatar" src="${avatarUrl}" onerror="this.src=getAvatarUrl('fallback')" />
                        <div class="session-info">
                            <div class="session-meta">
                                <span>${escapeAttr(lastDate)}</span>
                            </div>
                            <div class="session-name"></div>
                            <div class="session-last"></div>
                        </div>
                    `;
                    item.querySelector('.session-name').textContent = safeText(s.name);
                    item.querySelector('.session-last').textContent = formatSessionPreview(s.last_msg);
                    content.appendChild(item);
                });

                list.appendChild(header);
                list.appendChild(content);
            });

            // 自动选中第一个会话
            const firstSession = data.data[0];
            if (firstSession) {
                setTimeout(() => selectSession(firstSession.session_id, firstSession.name, firstSession.message_type), 100);
            }
        }
    } catch (e) { console.error(e); }
}

async function selectSession(sessionId, name, msgType) {
    if (activeSessionId === sessionId && activeUserId === '') {
        if (window.innerWidth <= 1400) {
            closeAllPanels();
        }
        document.querySelector(`.session-item[data-session-id="${CSS.escape(sessionId)}"]`)?.classList.add('active');
        return;
    }

    if (window.innerWidth <= 1400) {
        closeAllPanels();
    }
    activeMsgType = msgType || '';
    activeSessionId = sessionId;
    memberRequestSeq += 1;
    rankRequestSeq += 1;
    memberOffset = 0;
    memberTotal = 0;
    memberHasMore = false;
    rankOffset = 0;
    rankTotal = 0;
    rankHasMore = false;
    sidebarMemberUsers = [];
    rankMemberUsers = [];
    window.globalTopUsers = [];
    memberSearchKeyword = '';
    document.getElementById('activeSessionId').innerText = sessionId;

    document.querySelectorAll('.session-item').forEach(el => {
        if (el.dataset.sessionId === sessionId) el.classList.add('active');
        else el.classList.remove('active');
    });

    document.querySelectorAll('.sidebar-sub-menu').forEach(el => el.remove());
    if (activeUserId !== '') activeUserId = '';
    window.userMap = {};

    const activeItem = document.querySelector(`.session-item[data-session-id="${CSS.escape(sessionId)}"]`);
    if (activeItem && !isFriendSessionType()) {
        const subMenu = document.createElement('div');
        subMenu.className = 'sidebar-sub-menu';

        const searchBox = document.createElement('div');
        searchBox.className = 'sub-menu-search';
        const searchInput = document.createElement('input');
        searchInput.type = 'text';
        searchInput.id = 'memberSearch';
        searchInput.placeholder = '定位成员...';
        searchInput.addEventListener('click', (event) => event.stopPropagation());
        searchInput.oninput = (e) => debounceMemberSearch(e.target.value);
        searchBox.appendChild(searchInput);

        const userListContainer = document.createElement('div');
        userListContainer.id = 'userListContainer';

        subMenu.appendChild(searchBox);
        subMenu.appendChild(userListContainer);
        activeItem.after(subMenu);
    }

    currentPage = 1;
    reloadStats();
    fetchHistory();
}

function renderUserList(users = sidebarMemberUsers) {
    const container = document.getElementById('userListContainer');
    if (!container) return;
    const subMenu = container.closest('.sidebar-sub-menu');
    const fragment = document.createDocumentFragment();

    users.forEach(u => {
        window.userMap[u.user_id] = u.sender_name;
        const subItem = document.createElement('div');
        subItem.className = `sub-menu-item ${activeUserId === u.user_id ? 'active' : ''}`;
        subItem.dataset.userId = safeText(u.user_id);
        const wrap = document.createElement('span');
        wrap.style.cssText = 'display:flex; align-items:center; gap:0.4rem; overflow:hidden;';
        const avatar = document.createElement('img');
        const avatarKey = safeText(u.user_id);
        avatar.src = avatarResolvedCache.get(avatarKey) || getAvatarUrl(avatarKey);
        avatar.onerror = () => { avatar.src = getAvatarUrl('fallback'); };
        avatar.loading = 'lazy';
        avatar.decoding = 'async';
        avatar.style.cssText = 'width:20px; height:20px; border-radius:50%; flex-shrink:0; object-fit:cover;';
        const name = document.createElement('span');
        name.className = 'sub-name';
        name.style.cssText = 'white-space:nowrap; overflow:hidden; text-overflow:ellipsis;';
        name.textContent = safeText(u.sender_name);
        wrap.append(avatar, name);
        const count = document.createElement('small');
        count.style.opacity = '0.6';
        count.style.flexShrink = '0';
        count.textContent = safeCount(u.count);
        subItem.append(wrap, count);
        subItem.onclick = (e) => {
            e.stopPropagation();
            const previousUserId = activeUserId;
            if (activeUserId === u.user_id) {
                activeUserId = '';
                document.querySelectorAll('.sub-menu-item').forEach(el => el.classList.remove('active'));
            } else {
                activeUserId = u.user_id;
                document.querySelectorAll('.sub-menu-item').forEach(el => el.classList.remove('active'));
                subItem.classList.add('active');
            }
            if (window.innerWidth <= 1400) {
                closeAllPanels();
            }
            if (previousUserId === activeUserId) return;
            reloadStats();
            fetchHistory();
        };
        fragment.appendChild(subItem);
    });

    if (memberHasMore) {
        const more = document.createElement('button');
        more.type = 'button';
        more.className = 'member-load-more';
        more.textContent = `加载更多 (${Math.min(memberOffset + memberPageSize, memberTotal)}/${memberTotal})`;
        more.onclick = (event) => {
            event.stopPropagation();
            fetchMembers({ target: 'sidebar', keyword: memberSearchKeyword, offset: memberOffset, append: true });
        };
        fragment.appendChild(more);
    }

    container.replaceChildren(fragment);
    openSidebarSubMenu(subMenu);
}

async function fetchMembers({ target = 'both', keyword = '', offset = 0, append = false, limit = null } = {}) {
    if (!activeSessionId || isFriendSessionType()) return;
    const updateSidebar = target === 'sidebar' || target === 'both';
    const updateRank = target === 'rank' || target === 'both';
    const sidebarSeq = updateSidebar ? ++memberRequestSeq : memberRequestSeq;
    const rankSeq = updateRank ? ++rankRequestSeq : rankRequestSeq;
    const requestKeyword = updateRank && !updateSidebar ? '' : safeText(keyword).trim();
    const defaultLimit = !append && offset === 0 ? getInitialMemberLimit() : memberPageSize;
    const fetchLimit = Math.max(1, Math.min(100, safeCount(limit) || defaultLimit));
    let url = `/api/members?session_id=${encodeURIComponent(activeSessionId)}&limit=${fetchLimit}&offset=${offset}`;
    if (requestKeyword) url += `&keyword=${encodeURIComponent(requestKeyword)}`;
    if (filterStart) url += `&time_start=${filterStart}`;
    if (filterEnd) url += `&time_end=${filterEnd}`;

    try {
        const res = await fetchAPI(url);
        if (!res.success) return;
        const payload = res.data || {};
        const members = payload.members || [];
        preloadUserAvatars(members);

        if (updateSidebar && sidebarSeq === memberRequestSeq) {
            sidebarMemberUsers = append ? sidebarMemberUsers.concat(members) : members;
            window.globalTopUsers = sidebarMemberUsers;
            memberOffset = offset + members.length;
            memberTotal = safeCount(payload.total);
            memberHasMore = !!payload.has_more;
            renderUserList(sidebarMemberUsers);
        }

        if (updateRank && rankSeq === rankRequestSeq) {
            if (!append) memberAutoFillCount = 0;
            rankMemberUsers = append ? rankMemberUsers.concat(members) : members;
            rankOffset = offset + members.length;
            rankTotal = safeCount(payload.total);
            rankHasMore = !!payload.has_more;
            renderAnalysisMemberList(rankMemberUsers, rankHasMore, rankTotal);
        }
    } catch (e) {
        console.error(e);
    }
}

function refreshMembersForActiveSession() {
    const limit = getInitialMemberLimit();
    if (memberSearchKeyword) {
        fetchMembers({ target: 'rank', keyword: '', offset: 0, append: false, limit });
        fetchMembers({ target: 'sidebar', keyword: memberSearchKeyword, offset: 0, append: false, limit: memberPageSize });
    } else {
        fetchMembers({ target: 'both', keyword: '', offset: 0, append: false, limit });
    }
}

function scheduleAnalysisMemberAutofill() {
    if (memberAutoFillPending || memberAutoFillCount >= memberAutoFillMaxRequests) return;
    memberAutoFillPending = true;
    requestAnimationFrame(() => {
        memberAutoFillPending = false;
        autofillAnalysisMembers();
    });
}

function autofillAnalysisMembers() {
    if (!rankHasMore || activeUserId || isFriendSessionType()) return;

    const content = document.getElementById('analysisContent');
    const list = document.getElementById('rankList');
    const more = document.getElementById('rankLoadMore');
    const firstItem = list?.querySelector('.rank-item');
    if (!content || !list || !firstItem) return;

    const contentRect = content.getBoundingClientRect();
    const lastVisible = more && more.style.display !== 'none' ? more : list;
    const lastRect = lastVisible.getBoundingClientRect();
    const contentStyle = window.getComputedStyle(content);
    const bottomPadding = parseFloat(contentStyle.paddingBottom) || 0;
    const remainingSpace = contentRect.bottom - lastRect.bottom - bottomPadding;
    if (remainingSpace <= 12) return;

    const listStyle = window.getComputedStyle(list);
    const gap = parseFloat(listStyle.rowGap || listStyle.gap) || 0;
    const rowHeight = Math.max(1, firstItem.getBoundingClientRect().height + gap);
    const needed = Math.min(memberPageSize, Math.max(1, Math.ceil((remainingSpace + gap) / rowHeight)));

    memberAutoFillCount += 1;
    fetchMembers({
        target: 'rank',
        keyword: '',
        offset: rankOffset,
        append: true,
        limit: needed,
    });
}

function openSidebarSubMenu(subMenu) {
    if (!subMenu) return;
    const targetHeight = Math.min(subMenu.scrollHeight, 300);
    subMenu.style.setProperty('--submenu-height', `${targetHeight}px`);

    if (!subMenu.classList.contains('open')) {
        requestAnimationFrame(() => {
            subMenu.classList.add('open');
            const finishOpen = (event) => {
                if (event.target !== subMenu || event.propertyName !== 'max-height') return;
                subMenu.classList.toggle('scrollable', subMenu.scrollHeight > 300);
                subMenu.removeEventListener('transitionend', finishOpen);
            };
            subMenu.addEventListener('transitionend', finishOpen);
        });
    } else {
        subMenu.classList.toggle('scrollable', subMenu.scrollHeight > 300);
    }
}

function attachRankItemHandlers(root = document) {
    root.querySelectorAll('.rank-item').forEach(item => {
        item.addEventListener('click', () => {
            const nextUserId = item.getAttribute('data-user-id');
            if (!nextUserId) return;
            activeUserId = nextUserId;

            document.querySelectorAll('.sub-menu-item').forEach(el => {
                if (el.dataset.userId === nextUserId) el.classList.add('active');
                else el.classList.remove('active');
            });
            reloadStats();
            fetchHistory();
        });
    });
}

function renderMemberRankItems(users) {
    return users.map((u, idx) => {
        const rank = idx + 1;
        const div = document.createElement('div');
        div.innerText = u.sender_name;
        const safeName = div.innerHTML;

        let rankDisp = rank;
        if (rank === 1) rankDisp = '🥇';
        else if (rank === 2) rankDisp = '🥈';
        else if (rank === 3) rankDisp = '🥉';

        return `
            <div class="rank-item" data-rank="${rank}" data-user-id="${escapeAttr(u.user_id)}" data-user-name="${escapeAttr(u.sender_name)}">
                <div class="rank-number">${rankDisp}</div>
                <img src="${getAvatarUrl(u.user_id)}" class="rank-avatar" loading="lazy" decoding="async" onerror="this.src=getAvatarUrl('fallback')" />
                <div class="rank-info">
                    <div class="rank-name">${safeName}</div>
                    <div class="rank-count">${safeCount(u.count).toLocaleString()} 条消息</div>
                </div>
            </div>
        `;
    }).join('');
}

function renderAnalysisMemberList(users, hasMore, total) {
    const list = document.getElementById('rankList');
    const more = document.getElementById('rankLoadMore');
    if (!list) return;
    list.innerHTML = renderMemberRankItems(users);
    attachRankItemHandlers(list);
    if (more) {
        more.style.display = hasMore ? 'block' : 'none';
        more.textContent = `加载更多 (${Math.min(rankOffset + memberPageSize, total)}/${total})`;
        more.onclick = () => fetchMembers({ target: 'rank', keyword: '', offset: rankOffset, append: true });
    }
    scheduleAnalysisMemberAutofill();
}

async function reloadStats() {
    if (!activeSessionId) return;
    try {
        let qs = `/api/stats?session_id=${encodeURIComponent(activeSessionId)}`;
        if (activeUserId) qs += `&user_id=${encodeURIComponent(activeUserId)}`;
        if (isFriendSessionType()) qs += `&is_private=1`;
        if (filterStart) qs += `&time_start=${filterStart}`;
        if (filterEnd) qs += `&time_end=${filterEnd}`;

        const res = await fetchAPI(qs);
        if (res.success) {
            updateAnalysisPanel(res.data);
            if (!activeUserId && res.data.top_users) {
                refreshMembersForActiveSession();
            }
        } else {
            updateAnalysisPanel(null);
        }
    } catch (e) {
        updateAnalysisPanel(null);
    }
}

function renderBarChartUI(distribution) {
    if (!distribution || distribution.length !== 12) return '';
    const values = distribution.map(safeCount);
    let maxCount = Math.max(...values, 1);
    let html = `<div class="bar-chart-container animate-fade">`;
    for (let i = 0; i < 12; i++) {
        const value = values[i];
        let h = maxCount > 0 ? (value / maxCount) * 100 : 0;
        let timeLabel = `${i * 2}:00 - ${i * 2 + 2}:00`;
        html += `
            <div class="bar-wrapper">
                <div class="bar animate-grow-bar" style="height: 0%;" data-height="${h}%"></div>
                <div class="bar-tooltip">${timeLabel}<br/>${value.toLocaleString()}条</div>
                <div class="bar-label">${i * 2}</div>
            </div>
        `;
    }
    html += `</div>`;

    // Smooth transition growing delay in the next macrotask
    setTimeout(() => {
        document.querySelectorAll('.animate-grow-bar').forEach(bar => {
            const tgt = bar.getAttribute('data-height');
            if (tgt) {
                bar.style.height = tgt;
            }
        });
    }, 50);

    return html;
}

function updateAnalysisPanel(data) {
    const panel = document.getElementById('analysisPanel');
    const content = document.getElementById('analysisContent');
    if (!data) {
        panel.style.display = 'none';
        if (typeof analysisVisible !== 'undefined') analysisVisible = false;
        return;
    }

    panel.style.display = 'flex';
    if (typeof analysisVisible !== 'undefined') analysisVisible = true;
    let isIndividual = !!data.message_types;

    let html = '';
    if (isIndividual) {
        let activeDays = safeCount(data.active_days);
        let textLen = safeCount(data.avg_text_length);

        let peakTime = "无";
        let peakVal = 0;
        if (data.time_distribution) {
            for (let i = 0; i < 12; i++) {
                if (data.time_distribution[i] > peakVal) {
                    peakVal = data.time_distribution[i];
                    peakTime = `${i * 2}:00`;
                }
            }
        }

        html += `
            <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                <div class="stat-card" style="padding: 0.8rem 0.5rem;" title="这段时间内该成员发出的消息总条数">
                    <div class="value" style="font-size: 1.2rem;">${safeCount(data.total_messages).toLocaleString()}</div>
                    <div class="label">发言数</div>
                </div>
                <div class="stat-card" style="padding: 0.8rem 0.5rem;" title="刨除掉媒体与CQ等代码后，每发一条纯文字时的平均字符长度">
                    <div class="value" style="font-size: 1.2rem;">${textLen.toLocaleString()} 字</div>
                    <div class="label">均字长度</div>
                </div>
                <div class="stat-card" style="padding: 0.8rem 0.5rem;" title="真正有过发话记录的活跃天数">
                    <div class="value" style="font-size: 1.2rem;">${activeDays.toLocaleString()} 天</div>
                    <div class="label">活跃天数</div>
                </div>
                <div class="stat-card" style="padding: 0.8rem 0.5rem;" title="在一天之中按统计倾向概率最爱出没的高频时间起势">
                    <div class="value" style="font-size: 1.2rem;">${escapeAttr(peakTime)}</div>
                    <div class="label">巅峰出没期</div>
                </div>
            </div>
        `;
    } else {
        html += `
            <div style="display:flex; gap:10px;">
                <div class="stat-card" style="flex:1; padding: 1rem 0.5rem;">
                    <div class="value" style="font-size: 1.5rem;">${safeCount(data.total_messages).toLocaleString()}</div>
                    <div class="label">该时段发言数</div>
                </div>
                <div class="stat-card" style="flex:1; padding: 1rem 0.5rem;" title="无论选取何时间段，此项固定为选定记录全局今日总揽">
                    <div class="value" style="font-size: 1.5rem;">${safeCount(data.today_messages).toLocaleString()}</div>
                    <div class="label">全局今日数</div>
                </div>
            </div>
        `;
    }

    html += `
        <div style="margin-top:0.5rem;">
            <div class="section-title">📊 活跃时段分布</div>
            ${renderBarChartUI(data.time_distribution)}
        </div>
    `;

    if (isIndividual) {
        html += `<div style="margin-top: 0.5rem;"><div class="section-title">📝 消息形式分析</div><div class="type-list">`;
        data.message_types.forEach(t => {
            if (safeCount(t.value) > 0) {
                html += `
                    <div class="type-item">
                        <div class="type-name"><span>${escapeAttr(t.name)}</span></div>
                        <div class="type-value">${safeCount(t.value).toLocaleString()}</div>
                    </div>
                `;
            }
        });
        html += `</div></div>`;
    } else if (data.top_users && data.top_users.length > 0) {
        html += `
            <div style="margin-top: 0.5rem;">
                <div class="section-title">🏆 活跃成员排行</div>
                <div class="rank-list" id="rankList">${renderMemberRankItems(data.top_users.slice(0, getInitialMemberLimit()))}</div>
                <button type="button" class="member-load-more" id="rankLoadMore" style="display:none;">加载更多</button>
            </div>
        `;
    }

    content.innerHTML = html;

    if (!isIndividual && data.top_users && data.top_users.length > 0) {
        attachRankItemHandlers();
    }
}

// 优化后的滚动到底部函数：使用 ScrollIntoView 配合锚点，对移动端更友好
function scrollListToBottom(el) {
    const anchor = document.getElementById('scroll-anchor');
    if (anchor) {
        // 使用 behavior: 'auto' 确保瞬间触底，不给浏览器由于图片加载导致偏移的机会
        anchor.scrollIntoView({ behavior: 'auto', block: 'end' });
    } else {
        el.scrollTop = el.scrollHeight;
    }
}

function disablePrependAnimations(fragment) {
    fragment.querySelectorAll('.animate-fade').forEach(el => {
        el.classList.remove('animate-fade');
        el.style.animationDelay = '';
    });
}

function restorePrependAnchor(list, anchorEl, anchorTop) {
    if (!anchorEl || !anchorEl.isConnected || anchorTop === null) return;
    list.scrollTop += anchorEl.getBoundingClientRect().top - anchorTop;
}

function getPrependAnchor(list) {
    const listTop = list.getBoundingClientRect().top;
    const candidates = list.querySelectorAll('.message-group, .msg-system-center, .empty-state');
    for (const el of candidates) {
        const rect = el.getBoundingClientRect();
        if (rect.bottom > listTop + 8) {
            return el;
        }
    }
    return null;
}

function stabilizePrependAnchor(list, anchorEl, anchorTop, attempts = 5) {
    restorePrependAnchor(list, anchorEl, anchorTop);
    if (attempts <= 1) return;
    requestAnimationFrame(() => stabilizePrependAnchor(list, anchorEl, anchorTop, attempts - 1));
}

const MAX_RENDERED_MESSAGE_BLOCKS = 700;

function pruneMessageDom(list, removeFromBottom = false) {
    const blocks = Array.from(list.querySelectorAll('.message-group, .msg-system-center, .date-divider'));
    const overflow = blocks.length - MAX_RENDERED_MESSAGE_BLOCKS;
    if (overflow <= 0) return;

    const victims = removeFromBottom ? blocks.slice(-overflow) : blocks.slice(0, overflow);
    let removedAboveHeight = 0;
    const listTop = list.getBoundingClientRect().top;

    victims.forEach(el => {
        if (!removeFromBottom) {
            const rect = el.getBoundingClientRect();
            if (rect.bottom <= listTop) removedAboveHeight += rect.height;
        }
        el.remove();
    });

    // If we remove nodes above the viewport, compensate to avoid visible jumps.
    if (removedAboveHeight > 0) list.scrollTop -= removedAboveHeight;
}

// 监听容器高度变化（如图片加载），自动保持底部
const listObserver = new ResizeObserver(entries => {
    const list = document.getElementById('messageList');
    if (!list) return;

    // 如果用户距离底部小于 150px，则在内容高度变化时自动跟随后续增长
    const isNearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 150;
    if (isNearBottom) {
        scrollListToBottom(list);
    }
});

async function fetchHistory(append = false) {
    if (!activeSessionId) return;
    if (isHistoryLoading) return;
    isHistoryLoading = true;

    const list = document.getElementById('messageList');
    const loadMoreBtn = document.getElementById('loadMoreBtn');
    if (append && loadMoreBtn) loadMoreBtn.disabled = true;
    if (!append) {
        currentPage = 1;
        nextCursor = 0;
        // 开始监听高度变化
        listObserver.observe(list);
    }

    const keyword = document.getElementById('searchInput').value;

    try {
        let url = `/api/history?session_id=${encodeURIComponent(activeSessionId)}&user_id=${encodeURIComponent(activeUserId)}&keyword=${encodeURIComponent(keyword)}&page=${currentPage}&limit=${limit}`;
        if (append && nextCursor > 0) url += `&cursor=${nextCursor}`;
        if (filterStart) url += `&time_start=${filterStart}`;
        if (filterEnd) url += `&time_end=${filterEnd}`;

        const data = await fetchAPI(url);

        if (data.success) {
            if (data.next_cursor !== undefined) nextCursor = data.next_cursor;
            if (!append) {
                list.querySelectorAll('.message-group, .skeleton-group, .date-divider, .empty-state').forEach(el => el.remove());
            }

            if (data.data.length === 0 && !append) {
                const empty = document.createElement('div');
                empty.className = 'empty-state';
                empty.innerHTML = `
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
                    <p style="font-weight:600;">未找到相关记录</p>
                `;
                list.appendChild(empty);
                document.getElementById('loadMoreWrap').style.display = 'none';
                return;
            }

            // Reverse so oldest in batch comes first (for chronological top to bottom render)
            const messages = [...data.data].reverse();

            let renderUserId = null;
            let renderDateStr = null;
            let renderMessageCard = null;

            const fragment = document.createDocumentFragment();
            let loadMoreEl = document.getElementById('loadMoreWrap');

            messages.forEach(msg => {
                const dateStr = getDateStr(msg.timestamp);

                if (dateStr !== renderDateStr) {
                    const divider = document.createElement('div');
                    divider.className = 'date-divider animate-fade';
                    const dateLabel = document.createElement('span');
                    dateLabel.textContent = dateStr;
                    divider.appendChild(dateLabel);
                    fragment.appendChild(divider);
                    renderDateStr = dateStr;
                    renderUserId = null; // Next message forces new group wrapper
                }

                const bubble = createMessageBubble(msg);

                if (msg.user_id === '0' || msg.user_id === 0) {
                    // System message: center it
                    const systemMsg = document.createElement('div');
                    systemMsg.className = 'msg-system-center animate-fade';
                    systemMsg.innerHTML = `<span>${formatMsg(msg.message)}</span>`;
                    fragment.appendChild(systemMsg);
                    renderUserId = null; // Break grouping
                    renderMessageCard = null;
                } else if (renderUserId === msg.user_id && renderMessageCard) {
                    const bubbleList = renderMessageCard.querySelector('.msg-bubble-list');
                    bubbleList.appendChild(bubble);
                } else {
                    const group = document.createElement('div');
                    group.className = `message-group animate-fade${msg.is_right ? ' msg-right' : ''}`;
                    group.innerHTML = `
                        <div class="avatar-col">
                            <img class="msg-author-avatar" src="${getAvatarUrl(msg.user_id)}" onerror="this.src=getAvatarUrl('fallback')" />
                        </div>
                        <div class="content-col">
                            <div class="msg-author">
                                <span class="author-name"></span> 
                                <span class="author-id"></span>
                            </div>
                            <div class="msg-bubble-list"></div>
                        </div>
                    `;
                    group.querySelector('.author-name').textContent = safeText(msg.sender_name);
                    group.querySelector('.author-id').textContent = `#${safeText(msg.user_id)}`;
                    group.querySelector('.msg-bubble-list').appendChild(bubble);
                    fragment.appendChild(group);
                    renderMessageCard = group;
                    renderUserId = msg.user_id;
                }
            });

            const hasMore = !(data.has_more === false || data.data.length < limit);

            if (append) {
                const anchorEl = getPrependAnchor(list);
                const anchorTop = anchorEl ? anchorEl.getBoundingClientRect().top : null;
                disablePrependAnimations(fragment);

                // insert after loadMoreWrap
                if (loadMoreEl.nextSibling) {
                    list.insertBefore(fragment, loadMoreEl.nextSibling);
                } else {
                    list.appendChild(fragment);
                }
                loadMoreEl.style.display = hasMore ? 'block' : 'none';
                pruneMessageDom(list, true);
                stabilizePrependAnchor(list, anchorEl, anchorTop);
            } else {
                loadMoreEl.style.display = hasMore ? 'block' : 'none';
                list.appendChild(fragment);

                // 确保底部有一个永久锚点
                let anchor = document.getElementById('scroll-anchor');
                if (!anchor) {
                    anchor = document.createElement('div');
                    anchor.id = 'scroll-anchor';
                    list.appendChild(anchor);
                } else {
                    list.appendChild(anchor); // 移到最后
                }

                pruneMessageDom(list, false);

                // 立即滚动
                scrollListToBottom(list);
                // 延时一丁点时间再试一次，确保渲染首帧完成
                setTimeout(() => scrollListToBottom(list), 50);
            }
        }
    } catch (e) { console.error(e); }
    finally {
        isHistoryLoading = false;
        if (loadMoreBtn) loadMoreBtn.disabled = false;
    }
}

async function fetchStats() {
    try {
        const data = await fetchAPI('/api/stats');
        if (data.success) {
            document.getElementById('stat-total').innerText = safeCount(data.data.total_messages).toLocaleString();
            document.getElementById('stat-today').innerText = safeCount(data.data.today_messages).toLocaleString();
        }
    } catch (e) { }
}

function handleSearch() {
    currentPage = 1;
    fetchHistory();
}

const viewport = document.getElementById('messageList');
const scrollBtn = document.getElementById('scrollToBottomBtn');

let scrollTicking = false;
let scrollBtnVisible = false;

viewport.addEventListener('scroll', () => {
    if (scrollTicking) return;
    scrollTicking = true;
    requestAnimationFrame(() => {
        const shouldShow = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight > 500;
        if (shouldShow !== scrollBtnVisible) {
            scrollBtnVisible = shouldShow;
            scrollBtn.style.display = shouldShow ? 'flex' : 'none';
        }
        scrollTicking = false;
    });
}, { passive: true });

scrollBtn.onclick = () => {
    viewport.scrollTo({ top: viewport.scrollHeight, behavior: 'smooth' });
};

async function initApp() {
    if (API_KEY) {
        const ok = await ensureAuthCookie();
        if (!ok) return;
    }
    showAuth(false);

    // 移动端侧边栏由 CSS :checked + 全局点击关闭控制

    fetchStats();
    fetchSessions();
    if (!window.statsInterval) {
        window.statsInterval = setInterval(fetchStats, 60000);
    }
}

const loadMoreBtn = document.getElementById('loadMoreBtn');
function handleLoadMore() {
    if (isHistoryLoading) return;
    document.activeElement?.blur();
    closeAllPanels();
    currentPage++;
    fetchHistory(true);
}

loadMoreBtn.addEventListener('pointerdown', (event) => {
    event.preventDefault();
});
loadMoreBtn.addEventListener('mousedown', (event) => {
    event.preventDefault();
});
loadMoreBtn.addEventListener('pointerup', (event) => {
    if (event.pointerType !== 'mouse') {
        event.preventDefault();
        handleLoadMore();
    }
});
loadMoreBtn.onclick = handleLoadMore;

// Time Filters Logic
document.querySelectorAll('.time-btn').forEach(btn => {
    btn.onclick = () => {
        document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

        const range = btn.dataset.range;
        const now = new Date();
        now.setHours(23, 59, 59, 999);
        filterEnd = Math.floor(now.getTime() / 1000);

        if (range === 'all') {
            filterStart = 0; filterEnd = 0;
        } else if (range === 'today') {
            const start = new Date(now); start.setHours(0, 0, 0, 0);
            filterStart = Math.floor(start.getTime() / 1000);
        } else if (range === 'week') {
            const start = new Date(now);
            start.setDate(start.getDate() - (start.getDay() === 0 ? 6 : start.getDay() - 1));
            start.setHours(0, 0, 0, 0);
            filterStart = Math.floor(start.getTime() / 1000);
        } else if (range === 'month') {
            const start = new Date(now.getFullYear(), now.getMonth(), 1);
            filterStart = Math.floor(start.getTime() / 1000);
        } else if (range === 'year') {
            const start = new Date(now.getFullYear(), 0, 1);
            filterStart = Math.floor(start.getTime() / 1000);
        }

        document.getElementById('timeStart').value = '';
        document.getElementById('timeEnd').value = '';
        if (activeSessionId) {
            reloadStats();
            currentPage = 1;
            fetchHistory();
        }
    };
});

function handleCustomTime() {
    document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
    const tStart = document.getElementById('timeStart').value;
    const tEnd = document.getElementById('timeEnd').value;
    filterStart = tStart ? Math.floor(new Date(tStart).getTime() / 1000) : 0;
    filterEnd = tEnd ? Math.floor(new Date(tEnd).getTime() / 1000) : 0;
    if (activeSessionId) {
        reloadStats();
        currentPage = 1;
        fetchHistory();
    }
}

document.getElementById('timeStart').onchange = handleCustomTime;
document.getElementById('timeEnd').onchange = handleCustomTime;



// Pure JS Panel Control
function closeAllPanels() {
    const sidebar = document.querySelector('.sidebar');
    const analysis = document.querySelector('.analysis-panel');
    const overlay = document.getElementById('mobile-overlay');

    if (sidebar) sidebar.classList.remove('open');
    if (analysis) analysis.classList.remove('open');
    if (overlay) overlay.classList.remove('active');
}

document.addEventListener('DOMContentLoaded', () => {
    initApp();

    const loginBtn = document.getElementById('login-btn');
    const logoutBtn = document.getElementById('logout-btn');
    const apiKeyInput = document.getElementById('api-key-input');
    const searchInput = document.getElementById('searchInput');
    const btnSidebar = document.getElementById('btn-sidebar');
    const btnAnalysis = document.getElementById('btn-analysis');
    const btnCloseSidebar = document.getElementById('btn-close-sidebar');
    const btnCloseAnalysis = document.getElementById('btn-close-analysis');
    const overlay = document.getElementById('mobile-overlay');
    const sidebar = document.querySelector('.sidebar');
    const analysis = document.querySelector('.analysis-panel');

    if (loginBtn) {
        loginBtn.addEventListener('click', verifyLogin);
    }

    if (logoutBtn) {
        logoutBtn.addEventListener('click', logout);
    }

    if (apiKeyInput) {
        apiKeyInput.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') verifyLogin();
        });
    }

    if (searchInput) {
        searchInput.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') handleSearch();
        });
    }

    if (btnSidebar) {
        btnSidebar.addEventListener('click', () => {
            if (sidebar) sidebar.classList.add('open');
            if (overlay) overlay.classList.add('active');
        });
    }

    if (btnAnalysis) {
        btnAnalysis.addEventListener('click', () => {
            if (analysis) analysis.classList.add('open');
            if (overlay) overlay.classList.add('active');
            if (activeSessionId) reloadStats();
        });
    }

    if (btnCloseSidebar) {
        btnCloseSidebar.addEventListener('click', closeAllPanels);
    }

    if (btnCloseAnalysis) {
        btnCloseAnalysis.addEventListener('click', closeAllPanels);
    }

    if (overlay) {
        overlay.addEventListener('click', closeAllPanels);
    }
});
