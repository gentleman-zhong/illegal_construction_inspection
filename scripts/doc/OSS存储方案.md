# OSS存储方案

| 整改范围 | 状态 | 负责人 | 备注 |
| --- | --- | --- | --- |
| 基础版前端 | 进行中 |  |  |
| 综合治理前端 | 测试中 |  |  |
| 综合治理后端 | 测试中 |  |  |
| 虎视通用后端 | 测试中 |  |  |
| 派单前端 | 未开始 |  |  |
| 派单后端 | 未开始 |  | 数据独立存储 |
| 派单小程序 | 未开始 |  |  |
| 文旅小程序 | 未开始 |  |  |
| 文旅后端 | 未开始 |  |  |
| 第三方对接 | 未开始 | $\color{#0089FF}{@董亚琪}$  $\color{#0089FF}{@张羽丰}$ |  |
| 电力前后端 | 未开始 | $\color{#0089FF}{@赵爱涛}$ |  |
| 自动建图 | 未开始 | $\color{#0089FF}{@王笑天}$ | 模型大量文件<br>3dtiles还在磁盘<br>暂时还在磁盘，数据已经鉴权了 |
| 空间编码和设备 | 未开始 | $\color{#0089FF}{@孟宪旺}$  $\color{#0089FF}{@周国庆}$ | 大文件 |
| 飞机遥控器 | 未开始 | $\color{#0089FF}{@张羽丰}$ |  |

备注：样板间隔离（调研），用户隔离（调研），数据隔离（按环境，后续规划），断点续传，使用建图航线任务巡检、涉及照片事件，短信/钉钉通知的地址

一阶段：免认证，基础版、综合治理、通用模块上线，预计7.16

二阶段：认证，所有服务（前后端）、第三方上线，预计7.6开始整改，预计7.31上线

![image.png](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/res/ZWGl05mR17vjZn34/img/afe1b776-3364-4690-bbb8-f3f6ad851803.png)

![image.png](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/res/ZWGl05mR17vjZn34/img/391e32c6-c35b-4c22-a38a-760d0bb508e3.png)

## 一、方案概述

### 1.1 背景

为统一 OSS 资源访问入口、增强安全性，所有 OSS 文件访问已收口至网关代理。前端通过网关域名访问 OSS 文件，网关负责鉴权、转发。

### 1.2 架构说明

```plaintext
+---------+      +---------+      +---------+
|  前端    | ---> |  网关    | ---> |  OSS    |
| 浏览器  |      | api.xxx  |      | oss.xxx |
+---------+      +---------+      +---------+
                  鉴权 + 转发       浪潮oss存储服务

+---------+      +---------+
| 服务端  | ---> |  OSS    |  <-- 直连，无需网关
| 后端服务|      | oss.xxx |
+---------+      +---------+
```

*   **前端访问**：通过网关 URL（`https://api.xxx.com/oss/...`），网关自动鉴权并转发到 OSS
    
*   **服务端访问**：直接使用 OSS 直链（`https://oss.ikingtec.com/{bucket}/...`），无需经过网关
    

### 1.3 核心原则

> **数据库存储网关 URL，服务端访问时转为 OSS 直链。**

| 场景 | 使用的 URL | 说明 |
| --- | --- | --- |
| 前端展示图片/文件 | 网关 URL | 浏览器自带 Token，网关可鉴权 |
| 服务端下载/处理文件 | OSS 直链 | 服务端无用户 Token，需直连 OSS |
| 文件上传 | 网关 URL | 走统一上传接口 |

## 二、URL 转换规则

### 2.1 网关 URL 格式

```plaintext
https://api-test.ikingtec.com/oss/{objectPath}
```

*   `api-test.ikingtec.com`：网关域名（按环境变化）
    
*   `oss`：网关路由前缀（固定）
    
*   `{objectPath}`：OSS 对象路径，如 `odm-image/20260624/xxx.jpg`
    

### 2.2 OSS 直链格式

```plaintext
https://oss.ikingtec.com/{bucketName}/{objectPath}
```

*   `oss.ikingtec.com`：OSS 域名（固定）
    
*   `{bucketName}`：桶名，如 `hushi-test`（按环境变化）
    
*   `{objectPath}`：与网关 URL 中的对象路径一致
    

### 2.3 转换逻辑

```plaintext
网关URL:  https://api-test.ikingtec.com/oss/odm-image/xxx.jpg
          |--- host ----------||- prefix ||-- objectPath ------|

                | 去掉 host 和 prefix，拼接 ossEndpoint + bucketName

OSS直链:  https://oss.ikingtec.com/hushi-test/odm-image/xxx.jpg
          |--- ossEndpoint ---|| bucket ||-- objectPath ------|
```

## 三、配置说明

### 3.1 Nacos 完整配置

```yaml
iking:
  oss:
    bucket:
      hushi: hushi-test
    gateway:
      host: https://api-test.ikingtec.com
      bucket-prefix: oss
    enabled: true
    endpoint: http://10.230.0.5:8009
    host: https://oss.ikingtec.com
    region: us-east-1
    access-key: ****
    secret-key: ****
    path-style-access: false
    http:
      enabled: true
      prefix: /api
    encrypt:
      extension-suffix: ".doc,.docx,.xls,.xlsx,.ppt,.pptx,.pdf,.html,.txt,.csv,.zip,.jpg,.png"
      path-prefix: "mission-report/,mission-ai-report/,model-image/"
```

**注：不建议新增桶，在同一个桶下，使用不用的路径path区分即可**

### 3.2 配置项说明

| 配置项 | 说明 |
| --- | --- |
| `iking.oss.gateway.host` | 网关访问域名，用于判断 URL 是否为网关 URL |
| `iking.oss.gateway.bucket-prefix` | 桶名到网关前缀映射，转回直链时通过前缀反查桶名 |
| `iking.oss.host` | OSS 直链域名，拼接最终的直链地址 |
| `iking.oss.endpoint` | OSS 内部访问地址（SDK 直连用） |
| `iking.oss.access-key` / `secret-key` | OSS 访问凭证 |
| `iking.oss.bucket.hushi` | 默认桶名 |
| `iking.oss.encrypt` | 存储文件时设定的指定加密文件夹和文件后缀 |

### 3.3 环境对照

| 配置项 | 测试环境 | 生产环境 |
| --- | --- | --- |
| `iking.oss.gateway.host` | `https://api-test.ikingtec.com` | `https://api.ikingtec.com` |
| `iking.oss.host` | `https://oss.ikingtec.com` | `https://oss.ikingtec.com` |
| `iking.oss.endpoint` | `http://10.230.0.5:8009` | 按实际部署地址配置 |

## 四、各团队适配指引

### 4.1 需要做什么

1.  **引入** `**OssService**` **服务类**到项目中，用于文件上传
    
2.  **配置 Nacos**：添加 `iking.oss` 相关配置（参考第三章）
    
3.  **排查服务端代码**：找到所有通过 URL 访问 OSS 文件的地方，调用 `toDirectUrl()` 转换后再访问
    

### 4.2 文件上传说明

上传文件到 OSS 统一使用 `OssService`体参考`hushi-common-core`依赖，它会自动将文件上传到 OSS 并返回**网关 URL**，可直接存入数据库。

上传方式：

*   `saveFileToOss(MultipartFile, fileName, fileType)` - 上传 MultipartFile
    
*   `saveFileToOss(InputStream, fileName, fileType)` - 上传 InputStream
    
*   `saveFileToOss(byte[ ], fileName, fileType)` - 上传字节数组
    

其中`fileType`为文件类型枚举，具体参考`hushi-common-core`依赖的`FileTypeEnum`枚举类，决定存储路径前缀（如 `img/`、`doc/`、`drone-image/` 等）。

上传返回值为网关 URL 格式：`https://api-test.ikingtec.com/oss/{fileType}/{date}/{fileName}`

该 URL 直接存入数据库即可，前端可直接携带虎视token请求资源完成数据展示，服务端访问时通过 `toDirectUrl()` 转换。

### 4.3 适配方式

在所有 `new URL(url)` 之前，加一行转换：

```java
String directUrl = ossGatewayConfig.toDirectUrl(url);
```

`toDirectUrl` 具备幂等性：

*   传入网关 URL -> 返回 OSS 直链
    
*   传入 OSS 直链 -> 原样返回
    
*   传入本地路径 -> 原样返回
    

## 五、前端总体方案

### 5.1 文档范围与核心结论

本文说明虎视前端在 OSS/文件资源启用 Token 鉴权后的总体实现方案，覆盖普通图片、音频、文件下载、FLV 视频、上传回显以及部署联调要求。

原有图片、音频、视频和 ZIP 文件大量依赖浏览器标签直接访问 URL、`window.open(url)` 或原始 `<a href>` 下载。此类请求不会经过业务 Axios 拦截器，因此无法稳定携带 `Authorization`。当对象存储或文件流接口从匿名访问切换为 Token 鉴权后，容易出现 `401/403`、资源加载失败、视频重连异常或点击下载无后续动作。

本次改造的核心是新增公共工具 `src/utils/mediaBlob.js`：

*   图片、音频、普通下载文件通过带鉴权的 `fetch` 拉取资源内容，并转换为浏览器本地 `blob:` URL；
    
*   FLV 视频为保留 `Range`、流式播放和拖动能力，不整段下载为 Blob，而是把鉴权请求头传入播放器；
    
*   Token 仅允许发送给受信任的鉴权 Origin，避免泄露给第三方 OSS、CDN、地图服务或用户输入 URL；
    
*   所有 Blob URL 在资源切换、列表刷新、弹窗关闭或组件销毁时必须调用 `URL.revokeObjectURL()` 回收；
    
*   当前测试环境默认使用 `https://api-test.ikingtec.com/` 作为鉴权 Origin；独立 OSS 域名必须显式配置白名单。
    

---

### 5.2 总体访问链路

#### 5.2.1 普通图片、音频和下载文件

普通受保护资源采用“两段式请求”：

```plaintext
业务接口返回受保护资源 URL
        ↓
判断资源 URL 是否属于可信鉴权 Origin
        ↓
fetch(resourceUrl, { Authorization })
        ↓
response.blob()
        ↓
URL.createObjectURL(blob)
        ↓
img / audio / 预览组件 / a.download 使用 blob: URL
        ↓
资源切换或组件销毁时 URL.revokeObjectURL()
```

这种方式把需要鉴权的网络请求放在可控的 `fetch` 中，资源标签只消费浏览器本地生成的 `blob:` 地址。

原始资源 URL 仍应保留，例如 `_originUrl`、`_previewUrl`、`data-image-url` 等。Blob URL 只用于当前页面运行期展示，不能持久化、不能保存到数据库、不能跨会话复用。

#### 5.2.2 调整前后的访问差异

**调整前**

```plaintext
<el-image :src="item.imageUrl" />
<video :src="item.flvUrl" controls />
<audio><source :src="audioUrl"></audio>
```
```plaintext
window.open(zipUrl)
```

以上请求由浏览器标签、新窗口或原始链接直接发起，业务 Axios 拦截器不会参与，无法统一注入 `Authorization`。

**调整后**

```plaintext
原始资源 URL
→ 判断 URL Origin 是否在媒体鉴权范围
→ 读取登录 Token
→ fetch(url, { headers: { Authorization } })
→ response.blob()
→ URL.createObjectURL(blob)
→ img / audio / a.download 使用 blob:
→ 切换、下载结束或组件销毁时 URL.revokeObjectURL()
```

#### 5.2.3 FLV 视频访问链路

FLV 视频不建议整体转换为 Blob。原因是整段下载会影响大视频播放体验，并且不利于 `Range`、`206 Partial Content`、拖动进度和流式播放。

```plaintext
业务接口返回 FLV URL
        ↓
判断 URL 是否属于可信鉴权 Origin
        ↓
生成 requestHeaders.Authorization
        ↓
传入 LiveVideo / flv.js
        ↓
播放器以携带 Authorization、Range 的方式请求流
        ↓
401/403 停止重连；可恢复网络错误按策略重连
```
---

### 5.3 公共工具设计：`**src/utils/mediaBlob.js**`

#### 5.3.1 对外 API

| **方法** | **作用** |
| --- | --- |
| `getLoginToken()` | 按优先级读取登录 Token |
| `isBlobUrl(url)` | 判断是否为 `blob:` 地址 |
| `revokeBlobUrl(url)` | 仅释放有效的 `blob:` 地址 |
| `urlToBlobUrl(url, options)` | 带鉴权拉取资源并转换为 `blob:` URL |

#### 5.3.2 `**urlToBlobUrl**` 参数

| **参数** | **默认值** | **说明** |
| --- | --- | --- |
| `token` | `getLoginToken()` | 显式指定 Token；传空字符串可明确不使用 Token |
| `tokenPrefix` | `'Bearer'` | 鉴权前缀；接口要求原始 Token 时传 `''` |
| `timeout` | `30000` | 请求超时，单位毫秒 |
| `headers` | `{}` | 附加请求头 |
| `useToken` | `true` | 是否允许附加 Token；实际还受鉴权 Origin 白名单约束 |
| `fallbackToOriginal` | `true` | Blob 转换失败时是否返回原始 URL |
| `logError` | `false` | 是否输出资源转换错误 |

#### 5.3.3 Token 获取与 Blob 回收

```plaintext
export function getLoginToken() {
  var cookieToken = ''
  if (typeof document !== 'undefined') {
    var match = document.cookie.match(
      /(?:^|;\s*)hushiToken=([^;]*)/
    )
    cookieToken = match
      ? decodeURIComponent(match[1])
      : ''
  }
  return (
    cookieToken ||
    localStorage.getItem('hushiToken') ||
    localStorage.getItem('token') ||
    localStorage.getItem('access_token') ||
    ''
  )
}
export function isBlobUrl(url) {
  return typeof url === 'string' &&
    url.indexOf('blob:') === 0
}
export function revokeBlobUrl(url) {
  if (isBlobUrl(url)) {
    URL.revokeObjectURL(url)
  }
}
```

#### 5.3.4 鉴权 Origin 白名单

Token 不允许无条件发送给后端返回的任意 URL。公共工具按下列顺序读取允许携带 Token 的 Origin：

1.  `window.SITE_CONFIG.mediaAuthOrigins`：支持数组；
    
2.  `window.SITE_CONFIG.mediaAuthOrigin`：支持单个值或逗号分隔字符串；
    
3.  若未配置上述项，则回退到 `window.SITE_CONFIG.basePath` 的 Origin。
    

只有资源 URL 的 `origin` 命中白名单时才添加 `Authorization`。比较必须基于 `new URL(...).origin`，即严格比较协议、主机和端口，而非使用字符串包含关系。

```plaintext
function toOrigin(url) {
  try {
    return new URL(
      url,
      window.location.href
    ).origin
  } catch (e) {
    return ''
  }
}
function getMediaAuthOrigins() {
  var siteConfig = window.SITE_CONFIG || {}
  var configuredOrigins =
    siteConfig.mediaAuthOrigins ||
    siteConfig.mediaAuthOrigin
  var origins = []
  if (Array.isArray(configuredOrigins)) {
    origins = configuredOrigins
  } else if (configuredOrigins) {
    origins = String(configuredOrigins).split(',')
  } else if (siteConfig.basePath) {
    origins = [siteConfig.basePath]
  }
  return origins.map(function (origin) {
    return toOrigin(String(origin).trim())
  }).filter(Boolean)
}
function shouldUseToken(url, options) {
  if (options.useToken === false) return false
  try {
    var parsedUrl = new URL(
      url,
      window.location.href
    )
    return getMediaAuthOrigins()
      .indexOf(parsedUrl.origin) !== -1
  } catch (e) {
    return false
  }
}
function removeAuthorization(headers) {
  return Object.keys(headers).reduce(
    function (result, key) {
      if (key.toLowerCase() !== 'authorization') {
        result[key] = headers[key]
      }
      return result
    },
    {}
  )
}
```

以下 URL 不得命中白名单：

```plaintext
https://api-test.ikingtec.com.evil.example/...
https://evil.example/?next=api-test.ikingtec.com
```

安全要求：

*   禁止使用 `url.includes('api-test.ikingtec.com')` 判断域名；
    
*   默认不向公共 OSS、公共 CDN、地图服务、第三方资源或用户输入 URL 发送业务 Token；
    
*   新增 `mediaAuthOrigins` 前必须确认目标域名由可信服务控制，且确实需要虎视登录 Token；
    
*   不得把 Token 拼接在 query 参数中，避免泄露到浏览器历史、Referer、代理日志、截图或埋点；
    
*   日志、错误上报和埋点中不得记录完整 `Authorization`；
    
*   对必须鉴权的资源，不得因为失败就盲目去掉 Token 匿名重试。
    

#### 5.3.5 `**fetch**` 转 Blob URL

```plaintext
function responseToBlobUrl(response) {
  if (!response.ok) {
    throw new Error(
      'Resource load failed, status: ' +
      response.status
    )
  }
  return response.blob().then(function (blob) {
    return URL.createObjectURL(blob)
  })
}
function fetchBlobUrl(url, headers, signal) {
  return fetch(url, {
    method: 'GET',
    headers: headers,
    signal: signal
  }).then(responseToBlobUrl)
}
```

Blob URL 的准确来源为：

```plaintext
response.blob()
  .then(function (blob) {
    return URL.createObjectURL(blob)
  })
```

#### 5.3.6 `**urlToBlobUrl**` 完整入口

```plaintext
function shouldFallbackToOriginal(options) {
  return options.fallbackToOriginal !== false
}
function logResourceError(
  options,
  message,
  error
) {
  if (options.logError) {
    console.error(message, error)
  }
}
export function urlToBlobUrl(url, options) {
  options = options || {}
  if (!url) {
    return Promise.reject(
      new Error('Resource URL cannot be empty')
    )
  }
  if (isBlobUrl(url) || /^data:/.test(url)) {
    return Promise.resolve(url)
  }
  var useToken = shouldUseToken(url, options)
  var token = useToken
    ? (Object.prototype.hasOwnProperty.call(
        options, 'token'
      ) ? options.token : getLoginToken())
    : ''
  var tokenPrefix = options.tokenPrefix === undefined
    ? 'Bearer'
    : options.tokenPrefix
  var timeout = options.timeout || 30000
  var extraHeaders = options.headers || {}
  var controller = null
  var timer = null
  if (window.AbortController) {
    controller = new AbortController()
    timer = setTimeout(function () {
      controller.abort()
    }, timeout)
  }
  var headers = Object.assign({}, extraHeaders)
  if (useToken && token) {
    headers.Authorization =
      tokenPrefix &&
      token.indexOf(tokenPrefix + ' ') !== 0
        ? tokenPrefix + ' ' + token
        : token
  } else {
    headers = removeAuthorization(headers)
  }
  var signal = controller
    ? controller.signal
    : undefined
  return fetchBlobUrl(url, headers, signal)
    .catch(function (error) {
      logResourceError(
        options,
        'Resource convert to blobUrl failed:',
        error
      )
      if (shouldFallbackToOriginal(options)) {
        return url
      }
      throw error
    })
    .finally(function () {
      if (timer) clearTimeout(timer)
    })
}
```

#### 5.3.7 Token 格式约定

当前项目存在两类服务端约定：

```plaintext
Authorization: Bearer <token>
Authorization: <token>
```

公共工具默认添加 `Bearer`。已确认要求原始 Token 的调用点必须显式传入：

```plaintext
{
  token: this.$utils.getToken(),
  tokenPrefix: ''
}
```

新增接入点不能凭经验选择 Token 格式，应以目标网关和文件接口的实际约定为准。长期建议由网关统一 Token 格式，并在 `mediaBlob.js` 中集中管理，减少调用方差异。

#### 5.3.8 失败回退策略

`fallbackToOriginal` 默认值为 `true`，主要用于兼容公共资源或非受保护资源：Blob 转换失败后，可以让资源标签尝试原始 URL。

对于确定必须鉴权的图片、音频、下载文件或上传回显，应使用严格模式：

```plaintext
const blobUrl = await urlToBlobUrl(resourceUrl, {
  token: this.$utils.getToken(),
  tokenPrefix: '',
  fallbackToOriginal: false
})
```

否则浏览器会再次以不带 Token 的方式请求原始地址，既会产生无效请求，也可能造成错误态闪烁或资源权限策略被绕开。

---

### 5.4 组件接入规范与代码调整

#### 5.4.1 图片：列表缩略图与大图预览

##### 5.4.1.1 模板改造

**调整前**

```plaintext
<el-image
  :src="item.imageUrl"
  crossorigin="anonymous"
>
  <div slot="error" class="image-slot">
    <img src="@/assets/images/common/videoDefault.png">
  </div>
</el-image>
```

**调整后**

```plaintext
<el-image
  :src="item._imageBlobUrl || item.imageUrl"
  :data-image-url="item.imageUrl"
  crossorigin="anonymous"
>
  <div
    slot="error"
    class="image-slot"
    :data-image-url="item.imageUrl"
  >
    <img src="@/assets/images/common/videoDefault.png">
  </div>
</el-image>
```

##### 5.4.1.2 列表项状态约定

列表资源推荐保留以下状态：

```plaintext
item._blobSrc      // 发起转换时的原始 URL
item._blobUrl      // 转换成功的 blob: URL
item._blobLoading  // 加载状态，可用于 loading 展示
```

处理规则：

*   URL 未变化且已有转换结果时复用；
    
*   URL 改变前先释放旧 Blob；
    
*   Promise 返回时再次比较 `_blobSrc`，避免旧请求覆盖新数据；
    
*   结果已过期时，立即释放刚生成的 Blob；
    
*   列表刷新、弹窗关闭、组件销毁时批量释放；
    
*   必须鉴权资源加载失败时显示默认图，不回退为匿名原地址。
    

##### 5.4.1.3 原始图片转换并写回列表项

```plaintext
toBlobField(item, sourceKey, targetKey) {
  const url = item && item[sourceKey]
  if (!url || item[`${targetKey}Src`] === url) {
    return
  }
  revokeBlobUrl(item[targetKey])
  this.$set(item, targetKey, '')
  this.$set(item, `${targetKey}Src`, url)
  urlToBlobUrl(url, {
    fallbackToOriginal: false
  }).then(blobUrl => {
    if (item[`${targetKey}Src`] === url) {
      this.$set(item, targetKey, blobUrl)
    } else {
      revokeBlobUrl(blobUrl)
    }
  }).catch(() => {
    if (item[`${targetKey}Src`] === url) {
      this.$set(item, targetKey, '')
    }
  })
}
prepareMissionImages(mission) {
  if (!mission ||
      !Array.isArray(mission.missionEntityVos)) {
    return
  }
  mission.previewList = []
  mission.missionEntityVos.forEach((item, index) => {
    item.index = index
    const thumbUrl = item.thumbnailUrl || item.url
    item._url = this.$decryptLong
      .decryptFunction(thumbUrl)
    item._previewUrl = this.$decryptLong
      .decryptFunction(item.url)
    mission.previewList.push({
      url: item._previewUrl,
      _originUrl: item._previewUrl
    })
    this.toBlobField(item, '_url', '_blobUrl')
  })
  this.toPreviewBlobUrls(mission.previewList)
}
```

##### 5.4.1.4 大图预览与 Blob 回收

```plaintext
toPreviewBlobUrls(list) {
  if (!Array.isArray(list)) {
    return Promise.resolve()
  }
  return Promise.all(list.map(item => {
    const url = item &&
      (item._originUrl || item.url)
    if (!url || item._blobSrc === url) {
      return Promise.resolve()
    }
    revokeBlobUrl(item._blobUrl)
    this.$set(item, '_blobUrl', '')
    this.$set(item, '_blobSrc', url)
    return urlToBlobUrl(url, {
      fallbackToOriginal: false
    }).then(blobUrl => {
      if (item._blobSrc === url) {
        this.$set(item, '_blobUrl', blobUrl)
        this.$set(item, 'url', blobUrl)
      } else {
        revokeBlobUrl(blobUrl)
      }
    }).catch(() => {
      if (item._blobSrc === url) {
        this.$set(item, '_blobUrl', '')
        this.$set(item, 'url', url)
      }
    })
  }))
}
revokeBlobField(item, targetKey) {
  revokeBlobUrl(item && item[targetKey])
  if (item) {
    item[targetKey] = ''
    item[`${targetKey}Src`] = ''
  }
}
revokePreviewBlobUrls(list) {
  if (!Array.isArray(list)) return
  list.forEach(item => {
    revokeBlobUrl(item && item._blobUrl)
    if (item) {
      item.url = item._originUrl || item.url
      item._blobUrl = ''
      item._blobSrc = ''
    }
  })
}
revokeMissionBlobUrls(list = this.list) {
  if (!Array.isArray(list)) return
  list.forEach(mission => {
    ;(mission.missionEntityVos || [])
      .forEach(item => {
        this.revokeBlobField(item, '_blobUrl')
      })
    ;(mission.missionVideoDtoS || [])
      .forEach(item => {
        this.revokeBlobField(
          item,
          '_imageBlobUrl'
        )
      })
    ;(mission.defectList || [])
      .forEach(item => {
        this.revokeBlobField(
          item,
          '_thumbBlobUrl'
        )
      })
    this.revokePreviewBlobUrls(
      mission.previewList
    )
  })
}
beforeDestroy() {
  this.revokeMissionBlobUrls()
}
```

图片处理链路为：

```plaintext
解密原始 URL
→ urlToBlobUrl
→ _blobUrl / _imageBlobUrl
→ 模板 :src
→ 页面切换、关闭或销毁时 revoke
```

#### 5.4.2 音频：先转 Blob，再更新播放器

##### 5.4.2.1 模板、状态与格式判断

**调整前**

```plaintext
<audio ref="player" controls crossorigin>
  <source
    :src="audioUrl"
    type="audio/mpeg"
  >
</audio>
```

**调整后**

```plaintext
<audio ref="player" controls crossorigin>
  <source
    :src="currentAudioUrl"
    :type="audioType"
  >
</audio>
```
```plaintext
data() {
  return {
    player: null,
    currentAudioUrl: '',
    audioBlobUrl: '',
    audioBlobSrc: '',
    audioLoadKey: 0
  }
},
computed: {
  audioType() {
    const url =
      this.audioBlobSrc || this.audioUrl || ''
    const path = url
      .split('?')[0]
      .split('#')[0]
      .toLowerCase()
    if (path.endsWith('.aac')) return 'audio/aac'
    if (path.endsWith('.wav')) return 'audio/wav'
    return 'audio/mpeg'
  }
}
```

##### 5.4.2.2 获取、赋值、防串音与回收

```plaintext
watch: {
  audioUrl: {
    handler(val) {
      this.setAudioUrl(val)
    },
    immediate: true
  },
  currentAudioUrl(val) {
    if (val) this.updateAudio(val)
  }
},
methods: {
  setAudioUrl(audioUrl) {
    const loadKey = ++this.audioLoadKey
    this.revokeAudioBlobUrl()
    if (!audioUrl) {
      this.currentAudioUrl = ''
      return
    }
    if (isBlobUrl(audioUrl) ||
        /^data:/.test(audioUrl)) {
      this.currentAudioUrl = audioUrl
      return
    }
    this.audioBlobSrc = audioUrl
    urlToBlobUrl(audioUrl, {
      token: this.$utils.getToken(),
      tokenPrefix: '',
      fallbackToOriginal: false
    }).then(blobUrl => {
      if (
        loadKey === this.audioLoadKey &&
        this.audioBlobSrc === audioUrl
      ) {
        this.audioBlobUrl = blobUrl
        this.currentAudioUrl = blobUrl
      } else {
        revokeBlobUrl(blobUrl)
      }
    }).catch(() => {
      if (
        loadKey === this.audioLoadKey &&
        this.audioBlobSrc === audioUrl
      ) {
        this.currentAudioUrl = ''
      }
    })
  },
  revokeAudioBlobUrl() {
    revokeBlobUrl(this.audioBlobUrl)
    this.audioBlobUrl = ''
    this.audioBlobSrc = ''
  },
  updateAudio(audioUrl) {
    if (!this.player) return false
    this.player.source = {
      type: 'audio',
      sources: [{
        src: audioUrl,
        type: this.audioType
      }]
    }
  }
},
beforeDestroy() {
  if (this.player) {
    setTimeout(() => {
      this.player.destroy()
      this.player = null
    }, 3000)
  }
  this.revokeAudioBlobUrl()
}
```

要点：

*   `.aac` 使用 `audio/aac`；
    
*   `.wav` 使用 `audio/wav`；
    
*   `.mp3` 使用 `audio/mpeg`；
    
*   通过 `audioLoadKey` 防止快速切换资源时旧请求覆盖新结果；
    
*   组件销毁时必须销毁播放器并回收 Blob。
    

#### 5.4.3 下载文件：URL 下载与 Blob 下载分流

受保护资源不能再直接使用 `window.open(url)`，也不能直接把原始 URL 赋给 `<a href>`，因为这两种方式无法可靠补充 `Authorization`。

##### 5.4.3.1 业务接口返回受保护文件 URL

适用于单张图片、视频、任务报告、事件报告、Word/PDF、ZIP 和其他 OSS 文件。

```plaintext
调用业务接口
    ↓
得到受保护文件 URL
    ↓
urlToBlobUrl(URL) 携带 Token 下载文件内容
    ↓
创建 a 标签并设置 download
    ↓
触发点击
    ↓
释放 blob: URL
```

统一实现：

```plaintext
async downloadByBlob(url, name) {
  if (!url) return
  let fileName = name ||
    url.split('?')[0].split('/').pop() ||
    'download'
  try {
    fileName = decodeURIComponent(fileName)
  } catch (e) {}
  const blobUrl = await urlToBlobUrl(url, {
    fallbackToOriginal: false
  })
  const a = document.createElement('a')
  a.href = blobUrl
  a.download = fileName
  a.target = '_blank'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  revokeBlobUrl(blobUrl)
}
```

调用方必须 `await` 下载完成，确保批量下载状态、loading 和错误提示在文件内容真正获取后再结束。

##### 5.4.3.2 已有 `**imageZipUrl**` 的 ZIP 下载

**调整前**

```plaintext
download(url) {
  const a = document.createElement('a')
  a.href = url
  a.click()
}
async handleDownloadImage(item) {
  const {
    imageZipUrl,
    imageLoading
  } = item
  if (imageLoading) return
  item.imageLoading = true
  if (imageZipUrl) {
    setTimeout(() => {
      this.download(imageZipUrl)
      item.imageLoading = false
    }, 400)
    return
  }
}
```

**调整后**

```plaintext
async handleDownloadImage(item) {
  const {
    imageZipUrl,
    imageLoading,
    name
  } = item
  if (imageLoading) return
  item.imageLoading = true
  try {
    if (imageZipUrl) {
      await this.downloadByBlob(
        imageZipUrl,
        `${this.getZipName(name)}.zip`
      )
    }
  } finally {
    item.imageLoading = false
  }
}
```

##### 5.4.3.3 下载接口已直接返回 Blob

适用于后端直接返回 ZIP、二进制报告或已配置 `responseType: 'blob'` 的接口。此时不需要再次请求 `urlToBlobUrl`：

```plaintext
const responseBlob = await downloadApi(params)
const blob = responseBlob instanceof Blob
  ? responseBlob
  : new Blob([responseBlob])
const blobUrl = URL.createObjectURL(blob)
const a = document.createElement('a')
a.href = blobUrl
a.download = fileName
a.click()
URL.revokeObjectURL(blobUrl)
```

判断原则：

*   接口返回字符串 URL：仍需要第二次带 Token 获取文件；
    
*   接口已经返回 `Blob` 或二进制：直接创建本地 URL；
    
*   不要把 Blob 当作 URL 再请求；
    
*   不要把受保护 URL 当作已下载文件。
    

##### 5.4.3.4 批量下载、文件名与失败处理

批量图片、任务附件、缺陷文件可能由后端直接打包，也可能先生成 ZIP 地址：

*   后端返回 ZIP Blob：走“接口直接返回 Blob”；
    
*   后端返回 ZIP URL：走“受保护文件 URL”；
    
*   一次生成多个报告 URL：过滤空地址后逐个 `await` 下载；
    
*   下载期间锁定重复操作，在 `finally` 中恢复 loading；
    
*   单个失败进入统一错误处理，不影响后续错误提示和状态恢复。
    

文件名优先级建议：

1.  业务调用方显式传入的文件名；
    
2.  下载接口提供的文件名；
    
3.  URL `pathname` 的最后一段；
    
4.  默认值 `download`。
    

建议通过 `new URL(url, window.location.origin).pathname` 解析文件名，并对 `decodeURIComponent` 做异常兜底。不要直接按完整 URL 的 `/` 截取，以免 query、fragment 或编码字符进入文件名。

失败策略：

*   `401/403`：提示登录状态或权限异常，不再匿名重试；
    
*   `404`：提示文件不存在或已被清理；
    
*   超时/网络异常：恢复按钮状态，允许用户主动重试；
    
*   插入 DOM 的 `<a>` 必须在点击后移除；
    
*   只对 `blob:` 地址调用 `revokeBlobUrl`；
    
*   Blob URL 应在触发点击后释放，不应长期保存在业务列表中。
    

#### 5.4.4 URL 中的 `**#**` 处理

浏览器 URL 中的 `#` 表示 fragment，不会作为 HTTP 请求路径的一部分发送给服务端。对象 Key 本身包含 `#` 时，必须编码为 `%23`。

报告地址可在 `src/api/report.js` 返回前统一处理：

```plaintext
url.replace(/#/g, '%23')
```

注意：

*   对象 Key 中的 `#` 必须先编码；
    
*   PDF 阅读器的 `#zoom=page-fit` 属于前端 fragment，不应编码；
    
*   应先保证对象 Key 中的 `#` 已编码，再按展示需要追加 PDF 阅读器 fragment。
    

#### 5.4.5 FLV 播放：请求头透传与错误停止

##### 5.4.5.1 预览组件生成 Authorization

**调整前**

```plaintext
<LiveVideo
  type="flv"
  :url="messageData.url"
  controls
/>
```

**调整后**

```plaintext
<LiveVideo
  type="flv"
  :url="messageData.url"
  :request-headers="requestHeaders"
  @auth-error="$emit('auth-error', $event)"
  controls
/>
```
```plaintext
computed: {
  requestHeaders() {
    const headers = {
      ...(this.messageData.requestHeaders || {})
    }
    const token = getToken()
    if (
      token &&
      this.shouldAttachToken(this.messageData.url) &&
      !headers.Authorization
    ) {
      headers.Authorization = token
    }
    return headers
  }
},
methods: {
  shouldAttachToken(url) {
    try {
      const resourceOrigin =
        new URL(url, window.location.href).origin
      const apiOrigin = new URL(
        window.SITE_CONFIG.basePath,
        window.location.href
      ).origin
      return resourceOrigin === apiOrigin
    } catch (error) {
      return false
    }
  }
}
```

##### 5.4.5.2 请求头进入 `**flv.js**`

```plaintext
const player = flvjs.createPlayer(
  {
    type: 'flv',
    url: this.url,
    hasAudio: this.hasAudio,
    hasVideo: true,
    enableStashBuffer: false
  },
  {
    enableWorker: false,
    enableStashBuffer: false,
    reuseRedirectedURL: true,
    autoCleanupSourceBuffer: true,
    headers: this.requestHeaders
  }
)
```

##### 5.4.5.3 401/403 不再重复重连

```plaintext
handlePlayerError(
  player,
  errorType,
  errorDetail,
  errorInfo = {}
) {
  if (
    this.flvPlayer !== player ||
    this.isComponentDestroyed
  ) return
  const statusCode = Number(errorInfo.code)
  const isAuthorizationError =
    errorDetail ===
      flvjs.ErrorDetails.HTTP_STATUS_CODE_INVALID &&
    (statusCode === 401 || statusCode === 403)
  const isRetryable =
    errorType === flvjs.ErrorTypes.NETWORK_ERROR ||
    errorDetail ===
      flvjs.ErrorDetails.MEDIA_MSE_ERROR
  if (isAuthorizationError || !isRetryable) {
    this.transcoding = false
    this.imgError = true
    if (isAuthorizationError) {
      this.$emit('auth-error', {
        statusCode,
        errorType,
        errorDetail,
        errorInfo,
        url: this.url
      })
    }
    this.resetRetryState()
    this.detachMediaElement()
    return
  }
  this.scheduleReconnect()
}
```

错误策略：

*   `401/403`：认定为鉴权失败，触发 `auth-error`，停止播放和重连；
    
*   网络错误或可恢复 MSE 错误：按指数退避策略重连；
    
*   非可恢复错误：显示错误态并停止；
    
*   URL 切换、组件销毁或手动重载时：清理播放定时器、重连定时器和旧播放器实例；
    
*   视频封面仍按图片链路生成 `_imageBlobUrl`；
    
*   FLV 正文保持请求头透传，不整体下载为 Blob。
    

#### 5.4.6 上传与上传后回显

普通上传组件继续通过上传请求头携带 Token。

场景演绎的分片上传在 `fileChunkUpload` 中需要：

*   显式校验登录凭证；
    
*   设置 `Authorization`；
    
*   Token 缺失时直接拒绝上传；
    
*   不发送匿名上传请求。
    

上传完成后，接口返回的新资源地址仍需要按本文“两段式请求”转换，才能用于图片、视频或文件回显。

---

### 5.5 当前测试环境与前端部署配置

#### 5.5.1 `**api-test.ikingtec.com**` 鉴权域

```plaintext
const apiPrefix = 'https://api-test.ikingtec.com/'
window.SITE_CONFIG['basePath'] = apiPrefix
```

当前测试环境的 `basePath` 为：

```plaintext
https://api-test.ikingtec.com/
```

当受保护资源 URL 也是：

```plaintext
https://api-test.ikingtec.com/...
```

`mediaBlob.js` 会以 `basePath` 的 Origin 作为默认媒体鉴权 Origin，无需额外配置 `mediaAuthOrigins`。

等价逻辑为：

```plaintext
const resourceOrigin = new URL(
  resourceUrl,
  window.location.href
).origin
const authOrigin = 'https://api-test.ikingtec.com'
const shouldAttachToken =
  resourceOrigin === authOrigin
```

#### 5.5.2 独立 OSS 域名配置

如果 OSS 返回独立域名，必须显式加入白名单：

```plaintext
window.SITE_CONFIG['mediaAuthOrigins'] =
  'https://api-test.ikingtec.com,https://oss-test.example.com'
```

开发环境可以使用数组或逗号分隔字符串：

```plaintext
window.SITE_CONFIG.mediaAuthOrigins = [
  'https://api.example.com',
  'https://protected-media.example.com'
]
```

非开发环境的运行期配置会逐项解密，建议 `mediaAuthOrigins` 使用“加密后的逗号分隔字符串”，不要直接配置数组，除非已确认 `decryptFunction` 支持数组。

生产、预发和私有化环境不能硬编码测试域名，必须始终通过 `SITE_CONFIG` 获取当前环境的鉴权 Origin。

#### 5.5.3 当前测试环境规则

| **资源地址** | **是否附加 Token** | **处理方式** |
| --- | --- | --- |
| `https://api-test.ikingtec.com/...` | 是 | 携带 `Authorization` 获取资源 |
| 相对地址，解析后属于 `api-test.ikingtec.com` | 是 | 携带 `Authorization` 获取资源 |
| 其他 API/CDN/OSS/第三方域名 | 否 | 不附加业务 Token，按资源自身权限请求 |
| `blob:` / `data:` | 否 | 直接返回，不再发起网络请求 |
| 无法解析的 URL | 否 | 不附加 Token |

“其他域名直接请求”仅表示不附加业务 Token。普通图片或音频组件为了生成本地 Blob，仍可能匿名执行 `fetch`；若匿名转换失败且允许回退，才由资源标签加载原始 URL。对确认必须鉴权的资源，应设置 `fallbackToOriginal: false`。

#### 5.5.4 Token 来源与格式

| **项目** | **当前实现** | **部署要求** |
| --- | --- | --- |
| Token 来源 | Cookie `hushiToken`；随后 `localStorage.hushiToken`、`localStorage.token`、`localStorage.access_token` | 登录成功后至少写入其中一个；退出登录时清除 |
| 普通 API | Axios 拦截器：`Authorization = getToken()`，不自动加 `Bearer` | 网关需兼容当前原始 Token 格式 |
| Blob 工具 | 默认 `Authorization: Bearer <token>` | 若接口要求原始 Token，调用时传 `tokenPrefix: ''` |
| FLV 预览 | `requestHeaders.Authorization = getToken()` | 播放器必须透传请求头 |
| 第三方资源 | Origin 不匹配时移除 `Authorization` | 不得把虎视 Token 配置给无关域名 |

---

## 六、CORS 与 Range 联调要求

### 6.1 CORS 必要项

前端页面通常位于：

```plaintext
https://hushi-test.ikingtec.com
```

API 位于：

```plaintext
https://api-test.ikingtec.com
```

两者属于跨 Origin 请求。部署侧必须核对以下 CORS 配置：

```plaintext
Access-Control-Allow-Origin:
  精确允许 https://hushi-test.ikingtec.com
  多个前端域名时应按 Origin 白名单动态返回
Access-Control-Allow-Methods:
  至少包含 GET、HEAD、OPTIONS
Access-Control-Allow-Headers:
  至少包含 Authorization、Content-Type、Range
Access-Control-Expose-Headers:
  建议包含 Content-Length、Content-Range、Accept-Ranges
OPTIONS:
  不得要求业务 Token，应快速返回 204
视频 Range 请求：
  正确返回 206、Content-Range、Accept-Ranges
```

如资源使用独立 OSS 域名，该 OSS 域名同样需要支持上述跨域与 Range 能力。

---

## 七、发布联调检查

### 7.1 发布前检查

*   确认目标环境 `basePath` 正确指向当前 API 域；
    
*   若资源使用独立 OSS 域名，已将对应 Origin 加入 `mediaAuthOrigins`；
    
*   确认登录后可从 `hushiToken`、`token` 或 `access_token` 读取有效凭证；
    
*   确认网关同时或明确支持当前调用点使用的原始 Token / `Bearer Token` 格式；
    
*   浏览器 Network 中确认图片、ZIP、音频请求包含正确的 `Authorization`；
    
*   检查跨域预检 `OPTIONS` 是否成功；
    
*   检查 FLV 请求是否透传 `Authorization` 与 `Range`；
    
*   检查视频响应是否返回 `206`、`Content-Range`、`Accept-Ranges`；
    
*   确认第三方地图、公共 CDN、外部 OSS 请求中不携带虎视 Token。
    

## 八、验证与验收清单

### 8.1 验收用例

| **类别** | **用例** | **预期结果** |
| --- | --- | --- |
| 图片 | 缩略图加载、快速切换、关闭组件 | 携带 Token；不串图；Blob 被回收 |
| 图片预览 | 多图预览、切换、关闭弹窗 | 原始地址与 Blob 地址不混淆；关闭后回收 |
| 视频 | FLV 播放、拖动进度、失效 Token | 请求头透传；Range/206 正常；401/403 不持续重试 |
| 压缩包 | 基于已有 `imageZipUrl` 下载 ZIP | 认证 GET 成功；文件名正确；下载后 Blob 被回收 |
| 普通下载 | Word/PDF/报告/附件下载 | 下载 loading 状态正确；失败有提示；不匿名重试 |
| 音频 | MP3/AAC/WAV 播放并快速切换 | MIME 类型正确；无串音；旧 Blob 回收 |
| 上传 | 普通上传、分片上传、上传后回显 | 上传请求带 Token；无 Token 不上传；回显资源按两段式加载 |
| 安全边界 | 请求高德、天地图、公共 CDN、第三方 OSS | 请求中不携带虎视 Token |
| 配置 | 多环境配置、独立 OSS 域名 | 从 `SITE_CONFIG` 获取 Origin；不硬编码测试环境域名 |

---

## 九、风险、限制与回滚

### 9.1 风险与限制

| **风险** | **表现** | **处理建议** |
| --- | --- | --- |
| Token 格式不统一 | 部分请求使用 `Bearer`，部分使用原始 Token | 上线前抓包确认；长期由网关统一契约 |
| CORS 预检失败 | 控制台报 CORS，实际未发起 GET | 优先修复 `OPTIONS`、`Allow-Headers` 与 `Allow-Origin` |
| Range 不支持 | 视频无法拖动、播放中断或持续重连 | 网关透传 `Range`，后端正确返回 `206` |
| Blob 内存增长 | 长时间操作后内存升高 | 保持资源切换、弹窗关闭、组件销毁时的回收逻辑 |
| 异步竞态 | 快速切换图片或音频出现串图、串音 | 使用 `_blobSrc` / `audioLoadKey` 判断请求是否过期 |
| 默认回退原 URL | 鉴权 fetch 失败后再次匿名请求 | 敏感入口设置 `fallbackToOriginal: false` |
| 独立 OSS 未加入白名单 | 资源请求不携带 Token，导致 401 | 配置可信 `mediaAuthOrigins` 并重新联调 |
| 加密运行期配置不兼容 | 非 dev 环境配置读取失败 | 按既有加密发布流程生成配置字符串 |
| Token 泄露 | 业务 Token 发送到第三方地址 | 严格使用 `new URL(...).origin` 白名单判断 |

### 9.2 回滚建议

回滚遵循“网关先兼容、前端后回滚”的顺序：

1.  网关临时恢复旧资源匿名访问，或兼容原始 Token 与 `Bearer Token` 两种格式；
    
2.  再回滚前端版本；
    
3.  回滚后验证图片、音频、视频、ZIP 和普通下载均可访问。
    

不要只回滚前端而不处理网关鉴权策略，否则会重新触发浏览器标签直连资源、无法携带 `Authorization` 的原始问题。