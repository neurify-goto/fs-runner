# Playwrightにおける高度なCookie制御：ネットワーク傍受、スクリプト注入、同意バナー回避の決定版ガイド

## はじめに：PlaywrightにおけるCookie管理の現状

本稿は、Playwrightフレームワーク内でのCookieのブロックまたは管理方法を理解したいというユーザーの要望に応えるものです。特に「playwright-extraを使ったcookie blocker」という具体的な言及は、`puppeteer-extra`のプラグインエコシステムに精通しており、それに相当する使いやすい解決策を探していることを示唆しています。

まず、ユーザーの疑問に直接お答えします。`puppeteer-extra`が豊富なプラグインライブラリ（`puppeteer-extra-plugin-adblocker`や`puppeteer-extra-plugin-stealth`など）を誇るのとは対照的に、Playwrightのエコシステムには`playwright-extra-plugin-cookie-blocker`のような直接的な代替プラグインは確立されていません 。  

しかし、これはPlaywrightの限界を示すものではなく、むしろその設計思想の現れと捉えるべきです。Playwrightは、ネットワークとブラウザコンテキストを制御するための非常に強力な組み込みAPIを提供しており、多くのサードパーティ製プラグインを不要にしています。本稿は、これらのネイティブ機能を習得し、緻密で堅牢なCookie制御を実現するための決定版ガイドとして機能します。

本稿で扱う「Cookieブロッキング」とは、ユーザーが暗黙的に求める以下の3つの異なる目標を達成することを指します。

1.  **厳格なCookieの防止**: ウェブサイトからブラウザにCookieが保存されるのを完全に防ぎます。
    
2.  **同意バナーの処理**: 自動化を妨げるGDPR/CCPAなどのCookie同意ポップアップを自動的に承諾または回避します。
    
3.  **トラッカーと広告のブロック**: パフォーマンスとプライバシーを向上させるために、（Cookieを設定する）サードパーティのトラッキングスクリプトの読み込みを阻止します。
    

本稿では、まず基礎となる概念（ブラウザコンテキスト）から始め、高度な傍受技術（`page.route`、`addInitScript`）、そしてそれらの実践的な応用へと読者を導きます。

## 第I部 基礎概念：状態のサンドボックスとしてのブラウザコンテキスト

### 1.1. `BrowserContext`の中心的な役割

Playwrightにおけるセッション分離の核となるメカニズムは`BrowserContext`です 。これは、独自のCookie、ローカルストレージ、キャッシュを持つ、完全にサンドボックス化された「シークレットモード」のようなセッションと考えることができます。この分離は、すべてのCookie管理戦略の基盤となる基本的な概念です 。  

Playwrightの`BrowserContext` APIの設計は、暗黙的な状態の永続化よりも、_明示的な状態管理_というパラダイムを強く推奨しています。これは意図的なアーキテクチャ上の選択であり、単一のステートフルなブラウザプロファイルを再利用するアプローチと比較して、より堅牢で不安定さの少ない自動化スクリプトにつながります。各テストやタスクをそれぞれ新しいコンテキストで実行することで、完全なテスト分離を保証し、状態の漏洩を防ぐことができます。このアプローチは、前の失敗したテスト実行から残った状態（例えば、古いCookie）によってテストが失敗するという、自動化でよくある失敗のクラス全体を排除するのに役立ちます。

JavaScript

    // 新しいブラウザコンテキストを作成
    const context = await browser.newContext();
    
    // コンテキスト内に新しいページを作成
    const page = await context.newPage();
    await page.goto('https://example.com');
    
    // タスク完了後、コンテキストを閉じてすべてのセッションデータを破棄
    await context.close();

### 1.2. コアCookie管理API

`BrowserContext`オブジェクトで利用可能な主要なメソッドは、Cookieを直接操作するための完全なツールキットを提供します。

-   `context.cookies([urls])`: 現在のコンテキストからすべてのCookie、または特定のURLに関連するCookieを取得します 。  
    
-   `context.addCookies(cookies)`: 1つまたは複数のCookieをコンテキストに注入します。これは、第IV部で詳述する、同意バナーを事前に回避する戦略で極めて重要です 。  
    
-   `context.clearCookies()`: すべてのCookieを消去し、セッションの状態をリセットして、新規ユーザーの訪問をシミュレートします 。  
    

これらのAPIは、ログインセッションの維持のような一般的なシナリオで強力な効果を発揮します。例えば、一度ログインした後にCookieを抽出し、それを新しいコンテキストで復元することで、テストのたびにログインプロセスを繰り返す必要がなくなります 。  

JavaScript

    // シナリオ1：ログインしてセッションCookieを保存する
    const context1 = await browser.newContext();
    const page1 = await context1.newPage();
    await page1.goto('https://example.com/login');
    //... ログイン処理...
    const cookies = await context1.cookies();
    await context1.close();
    
    // シナリオ2：保存したCookieを使って新しいセッションを開始する
    const context2 = await browser.newContext();
    await context2.addCookies(cookies);
    const page2 = await context2.newPage();
    await page2.goto('https://example.com/dashboard'); // ログイン状態が維持される
    await context2.close();

## 第II部 ネットワークレベルの介入：リクエスト傍受（`page.route`）

### 2.1. Playwrightのネットワーク傍受入門

`page.route(url, handler)`および`context.route(url, handler)`は、Playwrightで最も強力なネットワーク機能です 。これにより、ブラウザが行うすべてのネットワークリクエスト（送信前）とすべてのレスポンス（処理前）をスクリプトで傍受できます。  

ハンドラ関数は`Route`オブジェクトと`Request`オブジェクトを受け取り、以下の主要なメソッドを使用してリクエストを制御します。

-   `route.continue()`: リクエストを（場合によっては変更を加えて）続行します。
    
-   `route.abort()`: リクエストを中止します。
    
-   `route.fulfill()`: カスタムレスポンスでリクエストを完了させます。
    

このネットワークレベルでの介入は、クライアントサイドのDOM操作よりも根本的に堅牢で効率的なCookieおよびトラッカー制御方法です。ウェブサイトのクライアントサイドコード（CSSセレクタやボタンのテキストなど）は頻繁に変更される可能性がありますが、トラッキングに使用されるドメイン（例：`google-analytics.com`）やCookieを設定するメカニズム（`Set-Cookie`ヘッダー）は、はるかに安定しています。`page.route`はこの安定的で基本的なネットワーク層で動作するため、高速で信頼性が高く、メンテナンスの少ない自動化スクリプトの構築において、アーキテクチャ的に優れたアプローチと言えます。

### 2.2. レスポンス改変による厳格なCookieブロッカーの実装

Cookieは主にHTTPレスポンスの`Set-Cookie`ヘッダーを介して設定されます。このメカニズムを標的にすることで、サーバーからのいかなるCookie設定の試みも無効化できます。

この技術では、すべてのレスポンスを傍受し（`page.route('**/*',...)`）、`route.fetch()`を使用して元のレスポンスを取得します。その後、レスポンスヘッダーを検査し、`Set-Cookie`ヘッダーを_除外した_ヘッダーセットで`route.fulfill()`を呼び出します。これにより、ブラウザはサーバーからのCookie設定指示を事実上無視することになります 。  

JavaScript

    await page.route('**/*', async route => {
      const response = await route.fetch();
      const headers = await response.allHeaders();
    
      // Set-Cookieヘッダーを削除
      delete headers['set-cookie'];
    
      await route.fulfill({
        response: response,
        headers: headers,
      });
    });
    
    // この設定後、ページはCookieを保存できなくなる
    await page.goto('https://example.com');

### 2.3. トラッキングスクリプトとドメインの選択的ブロック

トラッキングCookieをブロックする効果的な方法は、それらを設定するスクリプト自体をブロックすることです。これは`puppeteer-extra-plugin-adblocker`の機能に相当します 。  

`page.route()`でURLパターン（文字列、グロブ、正規表現）を使用して、既知の分析・広告ドメイン（例：`google-analytics.com`、`doubleclick.net`）へのリクエストを特定し、`route.abort()`で中止します。これにより、トラッカーが実行されるのを防ぎ、帯域幅を節約し、ページの読み込み速度を向上させることができます 。  

JavaScript

    // Google AnalyticsとDoubleclickのスクリプトをブロック
    await page.route(/google-analytics\.com|doubleclick\.net/, route => {
      console.log(`Blocking request to: ${route.request().url()}`);
      route.abort();
    });
    
    await page.goto('https://website-with-trackers.com');

### 2.4. コミュニティの広告ブロックリストの活用

個人のブロックリストを維持するのは困難です。より堅牢な解決策は、EasyListやEasyPrivacyのようなコミュニティによって維持されているフィルターリストをプログラムで利用することです。Node.jsやPythonのスクリプトでこれらのリスト（例えば、Fanboy's Cookie Monster List）を取得し、解析して`page.route()`で使用する正規表現やグロブパターンを動的に構築できます。これにより、Playwright内で強力かつ自己更新型の広告・トラッカーブロッカーを作成できます 。  

JavaScript

    import { chromium } from 'playwright';
    import fetch from 'node-fetch';
    
    async function createBlockerFromList(url) {
      const response = await fetch(url);
      const text = await response.text();
      const lines = text.split('\n').filter(line => line.trim() &&!line.startsWith('!'));
      // 簡単なドメインベースのブロッキング。より高度な実装にはAdBlockの構文解析が必要。
      const domains = lines.map(line => line.replace(/^\|\|/, '').replace(/\^$/, '')).filter(d => /^[a-zA-Z0-9.-]+$/.test(d));
      return new RegExp(domains.join('|'));
    }
    
    (async () => {
      const blockerRegex = await createBlockerFromList('https://secure.fanboy.co.nz/fanboy-cookiemonster.txt');
      
      const browser = await chromium.launch();
      const context = await browser.newContext();
      
      await context.route(blockerRegex, route => route.abort());
      
      const page = await context.newPage();
      await page.goto('https://www.example.com');
      
      await browser.close();
    })();

## 第III部 高度なクライアントサイド制御：初期化スクリプト（`addInitScript`）

### 3.1. DOM黎明期におけるスクリプト注入の理解

`context.addInitScript(script)`と`page.addInitScript(script)`は、特殊なタイミングでスクリプトを実行する機能を提供します 。このスクリプトは、ドキュメントが作成された  

_後_、かつページのどのスクリプトよりも_前_に実行されます。これにより、ページがJavaScript環境を利用する前に、その環境を改変する強力な能力が得られます。

ネットワーク傍受（`page.route`）とクライアントサイド注入（`addInitScript`）の選択は、_プロトコルレベルの制御_と_環境レベルの制御_という根本的なトレードオフを表します。`page.route`はブラウザエンジンに流入するデータを制御し、ページのJavaScript環境からは見えません。一方、`addInitScript`はページのJavaScriptが実行される環境自体を制御します。ウェブサイトは`Set-Cookie`ヘッダーが削除されたことを容易に検出できませんが、`document.cookie`が再定義されたことは検出できる可能性があります（例：`Object.getOwnPropertyDescriptor`をチェックする）。したがって、一般的なCookieやトラッカーのブロックには`page.route`が主要なツールであり、特定のクライアントサイドロジックを無効化する場合には`addInitScript`が専門的な手段となります。

### 3.2. 「Cookieブラックホール」の作成：`document.cookie`の無力化

`addInitScript`を使用して、`document.cookie`のゲッターとセッターを`Object.defineProperty()`で上書きするJavaScriptスニペットを注入できます。新しいセッターは何もせず（または試みをログに出力し）、ゲッターは空文字列を返します。これにより、ページ上のどのスクリプトにとっても`document.cookie`が事実上無力化されます。

JavaScript

    // addInitScriptで注入するスクリプト
    const script = `
      Object.defineProperty(document, 'cookie', {
        get: function() {
          console.log('A script tried to read cookies.');
          return '';
        },
        set: function(value) {
          console.log(\`A script tried to set a cookie: \${value}\`);
          return true; // 操作が成功したかのように振る舞う
        },
        configurable: true
      });
    `;
    
    await context.addInitScript(script);
    
    const page = await context.newPage();
    await page.goto('https://example.com');

### 3.3. ユースケースと注意点

この技術は、`Set-Cookie`ヘッダーに依存せず、ブラウザ内で完全にCookieを生成・管理するクライアントサイドスクリプトに対して最も効果的です。ネットワーク傍受が特定のサイトで複雑すぎる場合の代替策にもなり得ます。

ただし、これはDOMの改ざんであり、高度なボット対策システムはネイティブなブラウザオブジェクトや関数の変更を検出する可能性があります。この方法はネットワーク傍受よりも検出されやすいため、普遍的な解決策ではなく、強力だが専門的なツールとして考えるべきです。`puppeteer-extra-plugin-stealth`も同様の技術を用いて自動化を隠しますが、ここでは目的が異なります 。  

## 第IV部 実践的応用：Cookie同意バナーの攻略

同意バナーへの対処は画一的な問題ではありません。最適な解決策は、効率性と安定性の階層に従います。すなわち、事前設定（Proactive） > ブロック（Surgical） > 操作（Interactive）の順です。専門的なアプローチは、最も効率的な方法（事前設定）から始め、必要な場合にのみ安定性の低い方法にフォールバックすることです。この階層的な戦略により、スクリプトのパフォーマンスを最大化し、メンテナンスを最小限に抑えることができます。

### 4.1. プロアクティブアプローチ：同意Cookieの事前設定

これは多くの場合、最も効率的で信頼性の高い方法です。バナーと戦う代わりに、バナーが最初から表示されないようにします。

手順は以下の通りです。

1.  通常のブラウザで対象サイトを手動で訪問します。
    
2.  開発者ツールを開き、すべてのCookieをクリアします。
    
3.  Cookieバナーの「同意する」または「承諾する」ボタンをクリックします。
    
4.  「Application」>「Cookies」タブに移動し、同意を保存するために新たに設定されたCookie（例：`cookie_consent=true`、`gdpr=accepted`）を特定します 。  
    
5.  Playwrightスクリプトで、`page.goto()`を呼び出す_前_に、これらの同意Cookieの詳細を`context.addCookies()`で設定します。サイトは読み込み時にこれらのCookieを読み取り、同意が既に与えられていると判断するため、バナーは表示されません 。  
    

JavaScript

    const context = await browser.newContext();
    
    // サイトを訪問する前に同意Cookieを注入
    await context.addCookies([
      { name: 'cookieconsent_status', value: 'dismiss', domain: '.example.com', path: '/' }
    ]);
    
    const page = await context.newPage();
    await page.goto('https://example.com'); // 同意バナーは表示されないはず

### 4.2. 外科的アプローチ：ネットワークルーティングによるバナーのブロック

同意バナーが特定のスクリプトやAPIエンドポイントから読み込まれる場合、それをブロックすることができます。これは、`puppeteer-extra-plugin-adblocker`が`blockTrackersAndAnnoyances: true`オプションで機能する方法と類似しています 。  

開発者ツールを使用して、同意管理スクリプトを取得するネットワークリクエスト（例：`onetrust.com`、`cookiebot.com`からのリクエスト）を特定します。次に、`page.route()`を使用してこの特定のリクエストを`route.abort()`します。スクリプトがなければ、バナーはレンダリングされません。

JavaScript

    // サードパーティの同意管理サービスのスクリプトをブロック
    await page.route('**/*onetrust.com**', route => route.abort());
    
    await page.goto('https://example.com');

### 4.3. インタラクティブな代替策：UIのプログラム操作

上記の方法が失敗した場合や複雑すぎる場合の最終手段は、ユーザーのようにバナーを操作することです。堅牢なハンドラを記述するためのベストプラクティスは以下の通りです。

-   変更されにくい、広範かつ具体的なロケータを使用します（例：`page.getByRole('button', { name: /Accept|Agree|Consent/i })`）。
    
-   バナーが常に表示されるとは限らないため、短いタイムアウト（2〜5秒）を設定した`waitForSelector`を`try...catch`ブロック内で使用します 。  
    
-   バナーが`iframe`内にある可能性も考慮します。
    

JavaScript

    await page.goto('https://example.com');
    
    try {
      const acceptButton = page.getByRole('button', { name: /Accept All|Agree/i });
      await acceptButton.click({ timeout: 5000 });
    } catch (error) {
      console.log('Cookie consent banner not found or already handled.');
    }

## 第V部 統合と戦略的推奨事項

### 5.1. 意思決定フレームワーク：Cookie制御戦略の選択

この最終セクションでは、これまで議論してきたすべての技術を、実践的な意思決定フレームワークに統合します。特定の目標に基づいてどの方法を選択すべきかをユーザーに案内します。

-   **目標：パフォーマンス/スクレイピングの最大化**: `page.route`を使用して、不要なリソース（画像、CSS、トラッカー）をすべてブロックします 。  
    
-   **目標：厳格なプライバシー/Cookieなし**: `page.route`を使用して`Set-Cookie`ヘッダーを削除します。クライアントサイドのみのCookieに対しては、必要に応じて`addInitScript`を代替策として使用します。
    
-   **目標：特定の同意バナーの回避**: 第IV部で説明した階層的アプローチを使用します。まず事前設定を試み、次にネットワークブロッキング、最後にUI操作を試みます。
    

### 5.2. 戦略比較表

以下の表は、各アプローチのトレードオフを一目でわかるようにまとめたものです。この表は、技術的な詳細を、エンジニアが重視するパフォーマンス、安定性、保守性といった主要な意思決定基準に変換する戦略的なツールとして機能します。

戦略

メカニズム

粒度

パフォーマンスへの影響

ステルス性/検出可能性

長所

短所

最適なユースケース

**`page.route`によるヘッダー削除**

ネットワークレスポンスの改変

すべてのCookie

最小限/プラス

非常に低い

堅牢、プロトコルレベル、ページから不可視

広範すぎるとセッションベースのサイトを破壊する可能性

厳格なノーCookieポリシーの強制

**`addInitScript`による`document.cookie`の上書き**

クライアントサイドJS注入

すべてのクライアントサイドアクセス

軽微なオーバーヘッド

高い可能性

クライアントのみのロジックを捕捉

脆弱、検出可能

問題のあるクライアントサイドスクリプトの無効化

**`context.addCookies`による同意の事前設定**

コンテキスト状態の事前設定

特定のCookie

非常にプラス

該当なし（協調的）

同意処理で最速かつ最も安定

サイト固有の手動設定が必要

既知の同意バナーの信頼性の高い回避

**`page.route`によるドメインブロック**

ネットワークリクエストのブロック

特定のドメイン/スクリプト

プラス

低い

速度向上、トラッカーブロック

ブロックリストの維持が必要

汎用的な広告/トラッカーブロッキング

Google スプレッドシートにエクスポート

### 5.3. 最終的な専門家による推奨事項

-   PlaywrightのネイティブAPIは、Cookie管理のための完全かつ優れたツールキットを提供します。サードパーティのプラグインを探す前に、まずこれらの組み込み機能を習得することを推奨します。
    
-   自動化タスクには、最もシンプルで堅牢な解決策（既知のバナーには`context.addCookies`）から始め、必要な場合にのみより複雑な手法（`page.route`）にエスカレートしてください。
    
-   信頼性の高い自動化を作成するための基本的なベストプラクティスとして、すべての独立したタスクに分離された`BrowserContext`を使用することを強く推奨します。
    

結論として、Playwrightのネイティブ機能は、汎用的なサードパーティプラグインが提供できるものよりも強力でカスタマイズされたソリューションを構築する能力を開発者に与えます。これらのツールを習得することで、あらゆるCookie関連の課題に対処するための盤石な基盤が築かれます。



