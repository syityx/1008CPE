"""复制自原 send/0623fuzzypid.py 的 strategy 类。

原规则表、误差缩放、模糊推理、增量 PID 公式、整数截断全部保留。
interval_count 由发送端在收到视频时每 20 包推进，反馈处理持锁读取。
PID 输入 count0/count1 现在与链路 0/1 对齐，修复原接收端计数对调。
算法的调节方向、4.5 放大系数及整数输出可能导致较粗的分流变化，
为了便于对照实验，本版本不重新设计这些算法参数。
"""
interval_count = 0

class strategy():
    kp = 0.5
    ki = 0.5
    kd = 0.5
    erro_c = 0
    erro_pre = 0
    erro_ppre = 0
    qdetail_kp = 0
    qdetail_ki = 0
    qdetail_kd = 0
    num_area = 8
    qerro = 0  # 输入e对应论域中的值
    qerro_c = 0  # 输入de/dt对应论域中的值
    detail_kp = 0  # 输出增量kp
    detail_ki = 0  # 输出增量ki
    detail_kd = 0  # 输出增量kd
    e_membership_values = [-3, -2, -1, 0, 1, 2, 3]  # 输入erro的变化范围
    ec_membership_values = [-3, -2, -1, 0, 1, 2, 3]  # 输入de/dt（当前偏差erro与上一次偏差的差）的变化范围
    kp_menbership_values = [-3, -2, -1, 0, 1, 2, 3]  # 输出增量kp的隶属值
    ki_menbership_values = [-3, -2, -1, 0, 1, 2, 3]  # 输出增量ki的隶属值
    kd_menbership_values = [-3, -2, -1, 0, 1, 2, 3]  # 输出增量kd的隶属值
    fuzzyoutput_menbership_values = [-3, -2, -1, 0, 1, 2, 3]
    e_gradmembership = [0, 0]  # 输入e的隶属度
    ec_gradmembership = [0, 0]  # 输入de/dt的隶属度
    e_grad_index = [0, 0]  # 输入e隶属度在规则表的索引
    ec_grad_index = [0, 0]  # 输入de/dt隶属度在规则表的索引
    KpgradSums = [0, 0, 0, 0, 0, 0, 0]  # 输出增量kp总的隶属度
    KigradSums = [0, 0, 0, 0, 0, 0, 0]  # 输出增量ki总的隶属度
    KdgradSums = [0, 0, 0, 0, 0, 0, 0]  # 输出增量kd总的隶属度
    NB = -3
    NM = -2
    NS = -1
    ZO = 0
    PS = 1
    PM = 2
    PB = 3
    Kp_rule_list = [[PB, PB, PM, PM, PS, ZO, ZO],  # kp规则表
                    [PB, PB, PM, PS, PS, ZO, NS],
                    [PM, PM, PM, PS, ZO, NS, NS],
                    [PM, PM, PS, ZO, NS, NM, NM],
                    [PS, PS, ZO, NS, NS, NM, NM],
                    [PS, ZO, NS, NM, NM, NM, NB],
                    [ZO, ZO, NM, NM, NM, NB, NB]]

    Ki_rule_list = [[NB, NB, NM, NM, NS, ZO, ZO],  # ki规则表
                    [NB, NB, NM, NS, NS, ZO, ZO],
                    [NB, NM, NS, NS, ZO, PS, PS],
                    [NM, NM, NS, ZO, PS, PM, PM],
                    [NM, NS, ZO, PS, PS, PM, PB],
                    [ZO, ZO, PS, PS, PM, PB, PB],
                    [ZO, ZO, PS, PM, PM, PB, PB]]

    Kd_rule_list = [[PS, NS, NB, NB, NB, NM, PS],  # kd规则表
                    [PS, NS, NB, NM, NM, NS, ZO],
                    [ZO, NS, NM, NM, NS, NS, ZO],
                    [ZO, NS, NS, NS, NS, NS, ZO],
                    [ZO, ZO, ZO, ZO, ZO, ZO, ZO],
                    [PB, NS, PS, PS, PS, PS, PB],
                    [PB, PM, PM, PM, PS, PS, PB]]

    Fuzzy_rule_list = [[PB, PB, PB, PB, PM, ZO, ZO],
                       [PB, PB, PB, PM, PM, ZO, ZO],
                       [PB, PM, PM, PS, ZO, NS, NM],
                       [PM, PM, PS, ZO, NS, NM, NM],
                       [PS, PS, ZO, NM, NM, NM, NB],
                       [ZO, ZO, ZO, NM, NB, NB, NB],
                       [ZO, NS, NB, NB, NB, NB, NB]]

    buffer0 = []
    buffer1 = []
    precount = 0
    avebuffers0 = []
    avebuffers1 = []
    global interval_count

    def __init__(self):
        # 原版列表定义在类上；改成实例状态，避免多次运行/测试共享缓存。
        from copy import deepcopy
        from collections import deque
        for name, value in type(self).__dict__.items():
            if isinstance(value, (list, int, float)):
                setattr(self, name, deepcopy(value))
        self.avebuffers0 = deque(maxlen=100)
        self.avebuffers1 = deque(maxlen=100)

    def data_process(self, media0stock, media1stock):
        # print('interval_count = ',interval_count,'precount = ',self.precount,'len(self.buffer0)',len(self.buffer0),'len(self.buffer1)',len(self.buffer1))
        # print('len(self.buffer0)',len(self.buffer0),'len(self.buffer1)', len(self.buffer1))
        if interval_count == self.precount:
            self.buffer0.append(media0stock)
            self.buffer1.append(media1stock)
        elif len(self.buffer0) != 0 and len(self.buffer1) != 0:
            ave0 = int(sum(self.buffer0) / len(self.buffer0))
            ave1 = int(sum(self.buffer1) / len(self.buffer1))
            self.avebuffers0.append(ave0)
            self.avebuffers1.append(ave1)
            if len(self.avebuffers1) % 50 == 0:
                #print('avebuffers0', self.avebuffers0, 'avebuffers1', self.avebuffers1)
                #print('checkinggggggggggggggggggggggggg')
                pass
            if ave0 > ave1:
                # print('>>>>>>>>>>>>>>')
                increase = self.Fuzzy_PID_Increase(ave0, ave1)
            else:
                # print('<<<<<<<<<<<<<<')
                increase = -self.Fuzzy_PID_Increase(ave1, ave0)
            #print('interval_count = ', interval_count, 'precount = ', self.precount, 'increase = ', increase, 'G = ', G,'media0stock========',media0stock,'media1stock',media1stock)
            self.buffer0.clear()
            self.buffer1.clear()
            self.precount = interval_count
            return increase
        else:
            self.precount = interval_count
        return 9.9

    def Fuzzy_PID_Increase(self, pcc_buffersize, scc_buffersize):
        e_max = 4
        e_min = -4
        ec_max = 3
        ec_min = -3
        kp_max = 1
        kp_min = -1
        ki_max = 0.1
        ki_min = -0.1
        kd_max = 0.1
        kd_min = -0.1

        erro = (pcc_buffersize - 0) - (scc_buffersize - 0)  # 消除偏好
        erro = float(erro * 0.01)
        self.erro_c = erro - self.erro_pre
        u = int(self.FuzzyPIDcontroller(e_max, e_min, ec_max, ec_min, kp_max, kp_min, erro, self.erro_c, ki_max, ki_min,
                                        kd_max, kd_min, self.erro_pre, self.erro_ppre))
        self.erro_ppre = self.erro_pre
        self.erro_pre = erro

        return u  # in paper = G(t')

    def FuzzyPIDcontroller(self, e_max, e_min, ec_max, ec_min, kp_max, kp_min, erro, erro_c, ki_max, ki_min, kd_max,
                           kd_min, erro_pre, erro_ppre):
        # errosum += erro
        # Arear_dipart(e_max, e_min, ec_max, ec_min, kp_max, kp_min,ki_max,ki_min,kd_max,kd_min)
        self.qerro = self.Quantization(e_max, e_min, erro)
        self.qerro_c = self.Quantization(ec_max, ec_min, erro_c)
        self.Get_grad_membership(self.qerro, self.qerro_c)
        self.GetSumGrad()
        self.GetOUT()
        self.detail_kp = self.Inverse_quantization(kp_max, kp_min, self.qdetail_kp)
        self.detail_ki = self.Inverse_quantization(ki_max, ki_min, self.qdetail_ki)
        self.detail_kd = self.Inverse_quantization(kd_max, kd_min, self.qdetail_kd)
        self.qdetail_kd = 0
        self.qdetail_ki = 0
        self.qdetail_kp = 0

        self.kp = self.kp + self.detail_kp
        self.ki = self.ki + self.detail_ki
        self.kd = self.kd + self.detail_kd
        if (self.kp < 0):
            self.kp = 0
        elif (self.kp > 1):
            self.kp = 1
        if (self.ki < 0):
            self.ki = 0
        elif (self.ki > 1):
            self.ki = 1
        if (self.kd < 0):
            self.kd = 0
        elif (self.kd > 1):
            self.kd = 1

        self.detail_kp = 0
        self.detail_ki = 0
        self.detail_kd = 0
        output = self.kp * (erro - erro_pre) + self.ki * erro + self.kd * (erro - 2 * erro_pre + erro_ppre)
        # print('self.kp',self.kp,'self.ki',self.ki,'self.kd',self.kd,'erro',erro,'erro_pre',erro_pre)
        # print('erro',erro,'(erro - erro_pre)',(erro - erro_pre),'output',output)
        return output

    def Quantization(self, maximum, minimum, x):  # 假设x属于[minimum,maximum]，将其量化到[-3,3]的范围内
        qvalues = 6.0 * (x - minimum) / (maximum - minimum) - 3
        return qvalues

    def Inverse_quantization(self, maximum, minimum, qvalues):  # 将[-3,3]范围内的x恢复到[minimum,maximum]内
        x = (maximum - minimum) * (qvalues + 3) / 6 + minimum
        return x

    def Get_grad_membership(self, erro, erro_c):  # 求输入erro、erro_c的隶属度
        if (erro > self.e_membership_values[0] and erro < self.e_membership_values[6]):
            for i in range(self.num_area - 2):  # 分为6个区域
                if (erro >= self.e_membership_values[i] and erro <= self.e_membership_values[
                    i + 1]):  # 求输入分别在第i个区域和第i+1个区域的隶属度，并记录i，i+1取值
                    self.e_gradmembership[0] = -(erro - self.e_membership_values[i + 1]) / (
                                self.e_membership_values[i + 1] - self.e_membership_values[i])
                    self.e_gradmembership[1] = 1 + (erro - self.e_membership_values[i + 1]) / (
                                self.e_membership_values[i + 1] - self.e_membership_values[i])
                    self.e_grad_index[0] = i
                    self.e_grad_index[1] = i + 1
                    break
        else:  # 输入超出假设范围，完全隶属于某个区域
            if (erro <= self.e_membership_values[0]):
                self.e_gradmembership[0] = 1
                self.e_gradmembership[1] = 0
                self.e_grad_index[0] = 0
                self.e_grad_index[1] = -1
            elif (erro >= self.e_membership_values[6]):
                self.e_gradmembership[0] = 1
                self.e_gradmembership[1] = 0
                self.e_grad_index[0] = 6
                self.e_grad_index[1] = -1

        if (erro_c > self.ec_membership_values[0] and erro_c < self.ec_membership_values[6]):
            for i in range(self.num_area - 2):
                if (erro_c >= self.ec_membership_values[i] and erro_c <= self.ec_membership_values[i + 1]):
                    self.ec_gradmembership[0] = -(erro_c - self.ec_membership_values[i + 1]) / (
                                self.ec_membership_values[i + 1] - self.ec_membership_values[i])
                    self.ec_gradmembership[1] = 1 + (erro_c - self.ec_membership_values[i + 1]) / (
                                self.ec_membership_values[i + 1] - self.ec_membership_values[i])
                    self.ec_grad_index[0] = i
                    self.ec_grad_index[1] = i + 1
                    break
        else:
            if (erro_c <= self.ec_membership_values[0]):
                self.ec_gradmembership[0] = 1
                self.ec_gradmembership[1] = 0
                self.ec_grad_index[0] = 0
                self.ec_grad_index[1] = -1
            elif (erro_c >= self.ec_membership_values[6]):
                self.ec_gradmembership[0] = 1
                self.ec_gradmembership[1] = 0
                self.ec_grad_index[0] = 6
                self.ec_grad_index[1] = -1

    def GetSumGrad(self):
        for i in range(self.num_area - 1):
            self.KpgradSums[i] = 0
            self.KigradSums[i] = 0
            self.KdgradSums[i] = 0
        for i in range(2):
            if (self.e_grad_index[i] == -1):
                continue
            for j in range(2):
                if (self.ec_grad_index[j] != -1):
                    indexKp = self.Kp_rule_list[self.e_grad_index[i]][self.ec_grad_index[j]] + 3
                    indexKi = self.Ki_rule_list[self.e_grad_index[i]][self.ec_grad_index[j]] + 3
                    indexKd = self.Kd_rule_list[self.e_grad_index[i]][self.ec_grad_index[j]] + 3
                    # gradSums[index] = gradSums[index] + (e_gradmembership[i] * ec_gradmembership[j])* Kp_rule_list[e_grad_index[i]][ec_grad_index[j]]
                    self.KpgradSums[indexKp] = self.KpgradSums[indexKp] + (
                                self.e_gradmembership[i] * self.ec_gradmembership[j])
                    self.KigradSums[indexKi] = self.KigradSums[indexKi] + (
                                self.e_gradmembership[i] * self.ec_gradmembership[j])
                    self.KdgradSums[indexKd] = self.KdgradSums[indexKd] + (
                                self.e_gradmembership[i] * self.ec_gradmembership[j])
                else:
                    continue

    def GetOUT(self):
        for i in range(self.num_area - 1):
            self.qdetail_kp += self.kp_menbership_values[i] * self.KpgradSums[i]
            self.qdetail_ki += self.ki_menbership_values[i] * self.KigradSums[i]
            self.qdetail_kd += self.kd_menbership_values[i] * self.KdgradSums[i]
