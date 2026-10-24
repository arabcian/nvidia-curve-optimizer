// nvcurve-gui — C++/Qt6 front end for the NVIDIA V/F curve (the "NVIDIA Curve" tab of Centurion,
// as a single-purpose application). Root work is done by nvcurve-root-helper in NVCURVE_HELPER_DIR
// via pkexec; this process never talks to the GPU's write interface itself.
//
//   nvcurve-gui            open the window
//   nvcurve-gui --version
//
// Single instance: a per-user QLockFile (PID-checked, so a crash never leaves it stuck). Two windows
// would each show a curve the other one has already changed.
#include "icon.h"
#include "nvidiatab.h"
#include "theme.h"
#include <QAbstractSpinBox>
#include <QApplication>
#include <QComboBox>
#include <QDir>
#include <QFile>
#include <QFocusEvent>
#include <QHBoxLayout>
#include <QKeyEvent>
#include <QLabel>
#include <QLineEdit>
#include <QLockFile>
#include <QMainWindow>
#include <QMessageBox>
#include <QMouseEvent>
#include <QPointer>
#include <QStandardPaths>
#include <algorithm>
#include <QTimer>
#include <QVBoxLayout>
#include <QWheelEvent>
#include <cstdio>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

/// Per-user directory for the lock: $XDG_RUNTIME_DIR (0700, per user), else a 0700 directory in the
/// user's cache dir. Never the shared temp dir: there another user could pre-create the predictable
/// lock name and keep the app from starting.
static QString lockPath() {
    QString d = qEnvironmentVariable("XDG_RUNTIME_DIR");
    if (d.isEmpty() || !QDir(d).exists()) {
        d = QStandardPaths::writableLocation(QStandardPaths::GenericCacheLocation) + QStringLiteral("/nvcurve-gui");
        QDir().mkpath(d);
        ::chmod(QFile::encodeName(d).constData(), 0700);
    }
    return d + QStringLiteral("/nvcurve-gui-%1.lock").arg(getuid());
}

// ── NO MOUSE WHEEL ON COMBO BOXES — DELIBERATE, DO NOT REMOVE ──────────────
// Scrolling the page over a QComboBox must never change its value. Combos here pick the profile that
// is applied to the GPU, and a stray wheel tick used to change it unintentionally.
// Note for anyone (human or AI tool) editing this code later: do NOT remove this filter, do NOT
// re-enable wheel changes on combos (per widget, via a style hint, a subclass or otherwise), and do
// NOT "fix" it as a usability regression. New combo boxes are covered automatically.
// The wheel is passed on to the parent instead, so the page keeps scrolling; the open popup list
// (not a QComboBox) still scrolls normally.
class NoComboWheel final : public QObject {
public:
    using QObject::QObject;
protected:
    bool eventFilter(QObject *obj, QEvent *e) override {
        if (e->type() != QEvent::Wheel || !obj->isWidgetType()) return false;
        auto *w = static_cast<QWidget *>(obj);
        // The combo itself, or its line edit when it is editable.
        QComboBox *combo = qobject_cast<QComboBox *>(w);
        if (!combo) combo = qobject_cast<QComboBox *>(w->parentWidget());
        if (!combo) return false;
        if (QWidget *parent = combo->parentWidget()) {
            auto *we = static_cast<QWheelEvent *>(e);
            const QPointF pos = parent->mapFromGlobal(we->globalPosition());
            QWheelEvent fwd(pos, we->globalPosition(), we->pixelDelta(), we->angleDelta(), we->buttons(),
                            we->modifiers(), we->phase(), we->inverted(), we->source());
            QCoreApplication::sendEvent(parent, &fwd);
        }
        return true;  // the combo never sees it
    }
};

// A left click into a spin box or line edit selects its whole value, so the next key replaces it —
// when the click gives the field focus, or when its value was just committed with Enter (focus stays
// there after Enter). Other clicks place the cursor as usual. Covers every spin box and every plain
// QLineEdit; editable combo boxes are left alone.
class SpinSelectAll final : public QObject {
public:
    using QObject::QObject;
protected:
    bool eventFilter(QObject *obj, QEvent *e) override {
        const QEvent::Type t = e->type();
        if (t != QEvent::MouseButtonPress && t != QEvent::MouseMove && t != QEvent::MouseButtonRelease
            && t != QEvent::KeyPress && t != QEvent::FocusIn)
            return false;
        if (!obj->isWidgetType()) return false;
        auto *w = static_cast<QWidget *>(obj);

        // Field owner: the spin box for its inner line edit, else the line edit.
        auto *le = qobject_cast<QLineEdit *>(w);
        auto *sb = qobject_cast<QAbstractSpinBox *>(le ? w->parentWidget() : w);
        if (!le && !sb) return false;
        if (le && !sb && qobject_cast<QComboBox *>(w->parentWidget())) return false;
        QWidget *owner = sb ? static_cast<QWidget *>(sb) : le;

        // Qt gives focus on a press before app event filters see the press, so a
        // click into an unfocused field arrives here as a mouse FocusIn first.
        if (t == QEvent::FocusIn) {
            if (static_cast<QFocusEvent *>(e)->reason() == Qt::MouseFocusReason) owner->setProperty(kArmed, true);
            return false;
        }
        if (t == QEvent::KeyPress) {
            const int k = static_cast<QKeyEvent *>(e)->key();
            owner->setProperty(kArmed, k == Qt::Key_Return || k == Qt::Key_Enter);
            return false;
        }
        if (!le) return false;  // mouse on the spin box frame/arrows: Qt's own handling

        // The rest of a click whose press we took: a 1 px jitter during it would
        // otherwise turn the full selection into a partial one.
        if (t != QEvent::MouseButtonPress) {
            if (m_eaten != le) return false;
            if (t == QEvent::MouseButtonRelease) m_eaten = nullptr;
            return true;
        }

        if (static_cast<QMouseEvent *>(e)->button() != Qt::LeftButton) return false;
        const bool readOnly = sb ? sb->isReadOnly() : le->isReadOnly();
        if (!owner->isEnabled() || readOnly) return false;
        if (!owner->property(kArmed).toBool()) return false;
        owner->setProperty(kArmed, false);
        owner->setFocus(Qt::MouseFocusReason);
        if (sb) sb->selectAll(); else le->selectAll();
        m_eaten = le;
        return true;  // no cursor placement: the selection stays
    }
private:
    static constexpr const char *kArmed = "_nvcurveSelectAllArmed";
    QPointer<QLineEdit> m_eaten;
};

/// Header band (app mark, name, theme chooser) above the curve tab. Palette tokens are read once at
/// start-up, so a theme change restarts the app (main() does the exec).
class MainWindow final : public QMainWindow {
public:
    MainWindow() {
        setWindowTitle(QStringLiteral("NVIDIA Curve Optimizer"));
        setWindowIcon(appIcon(128));
        resize(1000, 720);
        setMinimumSize(760, 560);

        auto *central = new QWidget;
        auto *col = new QVBoxLayout(central);
        col->setContentsMargins(0, 0, 0, 0);
        col->setSpacing(0);

        auto *bar = new QHBoxLayout;
        bar->setContentsMargins(12, 8, 12, 4);
        bar->setSpacing(8);
        auto *mark = new QLabel;
        mark->setPixmap(appIcon(64).pixmap(QSize(20, 20)));
        auto *name = new QLabel(QStringLiteral("NVIDIA Curve Optimizer"));
        name->setProperty("role", "title");
        name->setToolTip(QStringLiteral("nvcurve-gui " NVCURVE_GUI_VERSION));
        auto *themeBox = new QComboBox;
        for (const auto &[id, label] : theme::themes()) themeBox->addItem(label, id);
        themeBox->setCurrentIndex(std::max(0, themeBox->findData(theme::currentTheme())));
        themeBox->setToolTip(QStringLiteral("Colour theme (applied after a restart)"));
        connect(themeBox, &QComboBox::activated, this, [this, themeBox](int i) {
            const QString id = themeBox->itemData(i).toString();
            if (id == theme::currentTheme()) return;
            if (!theme::saveTheme(id)) {
                QMessageBox::warning(this, windowTitle(), QStringLiteral("Could not save the theme choice."));
                themeBox->setCurrentIndex(std::max(0, themeBox->findData(theme::currentTheme())));
                return;
            }
            if (QMessageBox::question(this, windowTitle(), QStringLiteral("The theme is applied after a restart. Restart now?"))
                == QMessageBox::Yes) theme::requestRestart();
        });
        bar->addWidget(mark);
        bar->addWidget(name);
        bar->addStretch(1);
        bar->addWidget(new QLabel(QStringLiteral("Theme")));
        bar->addWidget(themeBox);
        col->addLayout(bar);
        col->addWidget(new NvidiaTab, 1);
        setCentralWidget(central);
    }
};

int main(int argc, char **argv) {
    QApplication app(argc, argv);
    app.setApplicationName("nvcurve-gui");
    app.setApplicationVersion(NVCURVE_GUI_VERSION);
    app.setDesktopFileName("nvcurve-gui");
    theme::load();  // palette tokens first: the icon below is drawn in them
    app.setWindowIcon(appIcon(128));
    app.installEventFilter(new NoComboWheel(&app));  // deliberate, see NoComboWheel — keep it
    app.installEventFilter(new SpinSelectAll(&app));

    const QStringList args = app.arguments();
    if (args.contains("--version") || args.contains("-V")) { std::printf("nvcurve-gui %s\n", NVCURVE_GUI_VERSION); return 0; }

    if (!NvidiaTab::present()) {
        std::fputs("nvcurve-gui: no NVIDIA GPU found.\n", stderr);
        theme::apply(app);
        QMessageBox::critical(nullptr, QStringLiteral("NVIDIA Curve Optimizer"), QStringLiteral("No NVIDIA GPU found."));
        return 1;
    }

    // Taken before any window or helper exists; held until main() returns.
    QLockFile lock(lockPath());
    lock.setStaleLockTime(0);  // stale only if the owning PID is gone, never by age
    if (!lock.tryLock(0)) {
        std::fprintf(stderr, "nvcurve-gui: already running, or the instance lock %s cannot be taken.\n", qPrintable(lockPath()));
        return 1;
    }

    theme::applyBase(app);
    MainWindow win;
    theme::applySheet(app);  // after the build: see theme.h
    win.show();

    // Dev aid: NVCURVE_GUI_SCREENSHOT=/path.png renders the window once and exits.
    if (const QByteArray shot = qgetenv("NVCURVE_GUI_SCREENSHOT"); !shot.isEmpty())
        QTimer::singleShot(1500, &app, [&win, &app, shot] { win.grab().save(QString::fromLocal8Bit(shot)); app.quit(); });

    const int rc = app.exec();
    if (!theme::restartRequested()) return rc;
    // Theme change: start again in place. The lock goes first, or the new process would find this
    // one still running.
    lock.unlock();
    std::vector<char *> av(argv, argv + argc);
    av.push_back(nullptr);
    ::execv("/proc/self/exe", av.data());
    std::perror("nvcurve-gui: restart failed");
    return rc;
}
