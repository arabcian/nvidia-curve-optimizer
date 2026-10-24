#include "icon.h"
#include "theme.h"
#include <QFile>
#include <QPainter>
#include <QSvgRenderer>

QIcon appIcon(int size) {
    // The installed nvcurve-gui.svg, recoloured to theme::ACCENT / BG1 so window and menu entry show
    // the same glyph. Every common size is rendered from the vector instead of scaling one bitmap.
    static const QByteArray src = [] {
        QFile f(QStringLiteral(":/nvcurve-gui.svg"));
        return f.open(QIODevice::ReadOnly) ? f.readAll() : QByteArray();
    }();
    QByteArray svg = src;
    svg.replace("#eba55b", theme::ACCENT).replace("#191a1e", theme::BG1);
    QSvgRenderer r(svg);
    QIcon icon;
    if (!r.isValid()) return icon;
    for (int s : {16, 22, 24, 32, 48, 64, 128, 256, size}) {
        QPixmap pm(s, s);
        pm.fill(Qt::transparent);
        QPainter p(&pm);
        p.setRenderHint(QPainter::Antialiasing);
        r.render(&p, QRectF(0, 0, s, s));
        p.end();
        icon.addPixmap(pm);
    }
    return icon;
}
