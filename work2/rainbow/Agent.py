import torch
import math
from tqdm import tqdm
from torch import optim, autograd
from IPython.display import clear_output
import matplotlib.pyplot as plt

from Network import RainbowCnnDQN
from common.replay_buffer import *

USE_CUDA = torch.cuda.is_available()
Variable = lambda *args, **kwargs: autograd.Variable(*args, **kwargs).cuda() if USE_CUDA else autograd.Variable(*args, **kwargs)


class Agent():
    def __init__(self, env, num_frames, batch_size, gamma, figp_dir, fig_dir):
        self.fig_dir = fig_dir
        self.figp_dir = figp_dir
        self.env = env
        self.num_atoms = 51
        self.Vmin = -10
        self.Vmax = 10

        self.num_frames = num_frames
        self.batch_size = batch_size
        self.gamma = gamma

        self.obs_dim = self.env.observation_space.shape
        self.act_dim = self.env.action_space.n

        self.replay_buffer = PrioritizedReplayBuffer(10000, alpha=0.6)
        self.current_model = RainbowCnnDQN(self.obs_dim, self.act_dim, self.num_atoms, self.Vmin, self.Vmax)
        self.target_model = RainbowCnnDQN(self.obs_dim, self.act_dim, self.num_atoms, self.Vmin, self.Vmax)

        if USE_CUDA:
            self.current_model = self.current_model.cuda()
            self.target_model = self.target_model.cuda()

        self.update_target(self.current_model, self.target_model)
        self.optimizer = optim.Adam(self.current_model.parameters(), lr=0.0001)

    def update_target(self, current_model, target_model):
        target_model.load_state_dict(current_model.state_dict())

    def projection_distribution(self, next_state, rewards, dones):
        batch_size = next_state.size(0)
        # 每个atom的距离deltaz，并获得{z0,z1,...,z_{num_atoms}}的support:size=atoms
        delta_z = float(self.Vmax - self.Vmin) / (self.num_atoms - 1)
        support = torch.linspace(self.Vmin, self.Vmax, self.num_atoms)
        # (batch_size,actions,atoms)*(atoms) = (batch_size,actions,atoms)
        next_dist = self.target_model(next_state).data.cpu() * support
        # 选峰值作为next_action
        next_action = next_dist.sum(2).max(1)[1]
        next_action = next_action.unsqueeze(1).unsqueeze(1).expand(next_dist.size(0), 1, next_dist.size(2))
        next_dist = next_dist.gather(1, next_action).squeeze(1) # (batch_size,atoms)

        rewards = rewards.unsqueeze(1).expand_as(next_dist)
        dones = dones.unsqueeze(1).expand_as(next_dist)
        support = support.unsqueeze(0).expand_as(next_dist)
        # Tz=r+gamma*Z
        Tz = rewards.cpu() + (1 - dones.cpu()) * self.gamma * support.cpu()
        Tz = Tz.clamp(min=self.Vmin, max=self.Vmax)
        b = (Tz - self.Vmin) / delta_z
        l = b.floor().long()
        u = b.ceil().long()
        # 偏移
        offset = torch.linspace(0, (batch_size - 1) * self.num_atoms, batch_size).long() \
            .unsqueeze(1).expand(batch_size, self.num_atoms)
        # 将dist投影到对应的位置
        proj_dist = torch.zeros(next_dist.size())
        proj_dist.view(-1).index_add_(0, (l + offset).view(-1), (next_dist * (u.float() - b)).view(-1))
        proj_dist.view(-1).index_add_(0, (u + offset).view(-1), (next_dist * (b - l.float())).view(-1))

        return proj_dist

    def compute_td_loss(self, batch_size, beta):
        # sample一组batch_size大小的样本
        state, action, reward, next_state, done, weights,\
        indices = self.replay_buffer.sample(batch_size, beta)

        state = Variable(torch.FloatTensor(np.float32(state)))
        next_state = Variable(torch.FloatTensor(np.float32(next_state)))
        action = Variable(torch.LongTensor(action))
        reward = Variable(torch.FloatTensor(reward))
        done = Variable(torch.FloatTensor(np.float32(done)))
        weights = Variable(torch.FloatTensor(weights))
        # 更新的distributional映射到对应的范围内 proj_dist=r+gamma*Z
        proj_dist = self.projection_distribution(next_state, reward, done)
        dist = self.current_model(state)
        action = action.unsqueeze(1).unsqueeze(1).expand(self.batch_size, 1, self.num_atoms)
        dist = dist.gather(1, action).squeeze(1)
        dist.data.clamp_(0.01, 0.99)
        # 使用KL散度计算loss = sum(p*log(p/q))
        loss =  -((Variable(proj_dist) * dist.log()).sum(-1))
        # 经验回放池的优先级同样使用KL散度进行更新
        self.replay_buffer.update_priorities(indices, loss.detach().squeeze().abs().cpu().numpy().tolist())
        loss = loss.mean()
        # 梯度反向传播
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
        # reset noise
        self.current_model.reset_noise()
        self.target_model.reset_noise()

        return loss

    def plot(self, frame_idx, rewards, losses):
        """
            绘制reward和loss的曲线图
        """
        clear_output(True)
        fig = plt.figure(figsize=(20, 5))
        plt.subplot(131)
        plt.title('frame %s. reward: %s' % (frame_idx, np.mean(rewards[-10:])))
        plt.plot(rewards)
        plt.subplot(132)
        plt.title('loss')
        plt.plot(losses)
        # plt.show()
        return fig

    def train(self):
        """
            模型训练过程
        """
        losses = []
        all_rewards = []
        episode_reward, episode, loss = 0, 1, 0
        state = self.env.reset()
        # epsilon-greedy策略
        epsilon_start = 1.0
        epsilon_final = 0.01
        epsilon_decay = 30000
        epsilon_by_frame = lambda frame_idx: epsilon_final + (epsilon_start - epsilon_final) * math.exp(
            -1. * frame_idx / epsilon_decay)

        beta_start = 0.4
        beta_frames = 100000
        beta_by_frame = lambda frame_idx: min(1.0, beta_start + frame_idx * (1.0 - beta_start) / beta_frames)

        # 进度条：循环遍历num_frames步
        pbar = tqdm(range(1, self.num_frames+1))
        for frame_idx in pbar:
            # 更新epsilon
            epsilon = epsilon_by_frame(frame_idx)
            # 依据epsilon-greedy策略选择动作
            action = self.current_model.act(state, epsilon)
            # 与环境交互一步
            next_state, reward, done, _ = self.env.step(action)
            # 将新的信息放入经验回放池
            self.replay_buffer.push(state, action, reward, next_state, done)
            # 更新状态
            state = next_state
            episode_reward += reward
            # 一轮episode结束
            if done:
                state = self.env.reset()
                all_rewards.append(episode_reward)
                episode_reward = 0
                episode += 1

            # 计算beta和loss
            if len(self.replay_buffer) > self.batch_size:
                beta = beta_by_frame(frame_idx)
                loss = self.compute_td_loss(self.batch_size, beta)
                losses.append(loss.item())

            # 保存数据
            if frame_idx % 50000 == 0:
                fig = self.plot(frame_idx, all_rewards, losses)
                # 保存绘图数据
                reward_tmp = np.array(all_rewards)
                losses_tmp = np.array(losses)
                np.save(self.fig_dir + 'rainbow_reward_' + '%d.npy' % frame_idx, reward_tmp)  # 保存为.npy格式
                np.save(self.fig_dir + 'rainbow_loss_' + '%d.npy' % frame_idx, losses_tmp)  # 保存为.npy格式
                #保存图片
                fig.savefig(self.figp_dir + 'rainbow_' + '%d.png' %frame_idx)

            # 更新target_Q = current_Q
            if frame_idx % 1000 == 0:
                self.update_target(self.current_model, self.target_model)

            pbar.set_description("episode:%d frame_id:%d loss:%4f reward:%4f" % (episode, frame_idx, loss, episode_reward))

