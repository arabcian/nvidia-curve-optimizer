#pragma once
#include "gpustress.h"

#include <QElapsedTimer>
#include <QJsonObject>
#include <QWidget>
#include <functional>

class QComboBox;
class QLabel;
class QProcess;
class QPushButton;
class QSpinBox;
class QTimer;
class QTreeWidget;

class GpuStressBox : public QWidget {
    Q_OBJECT
public:
    GpuStressBox(std::function<bool()> canStart, std::function<QString(const QString &)> curveFor, QWidget *parent = nullptr);
    void refresh(bool blocked);

Q_SIGNALS:
    void runningChanged(bool running);

private:
    void loadDevices();
    void showDevices();
    void start();
    void launch(const gpustress::Plan &p, gpustress::Mode m, int seconds);
    void readOutput();
    void finished(int exitCode, bool crashed);
    void tick();
    void fill();
    void setIdle(bool idle);
    void note(const QString &text, const char *color);

    std::function<bool()> canStart_;
    std::function<QString(const QString &)> curveFor_;
    QString program_;
    QComboBox *device_ = nullptr, *mode_ = nullptr, *minutes_ = nullptr;
    QSpinBox *percent_ = nullptr;
    QLabel *percentLabel_ = nullptr, *status_ = nullptr, *klogNote_ = nullptr;
    QPushButton *run_ = nullptr;
    QTreeWidget *results_ = nullptr;
    QProcess *proc_ = nullptr, *listProc_ = nullptr;
    QTimer *ticker_ = nullptr;
    QElapsedTimer elapsed_;
    QByteArray partial_;
    gpustress::Run run_state_;
    QList<gpustress::Device> devices_;
    QString pci_, deviceName_, curve_;
    gpustress::Mode runMode_ = gpustress::Mode::Vram;
    int seconds_ = 0;
    bool cancelled_ = false, blocked_ = false;
};
